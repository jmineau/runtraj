"""
Integration tests for runtraj end-to-end execution.

These tests run real HYSPLIT against real met files and are collected by
default.  They are automatically skipped when the met data
is not present (the ``met_dir`` fixture handles this).

Run integration tests explicitly:
    pytest tests/ -v -m integration

Skip them:
    pytest tests/ -m "not integration"
"""

import numpy as np
import pandas as pd
import pytest

from runtraj import Project
from runtraj.exceptions import SimulationError
from runtraj.execution.runner import resolve_compute_root
from runtraj.models.hysplit import FailureReason
from runtraj.releases import MultiPointRelease

from ..conftest import integration
from .conftest import REFERENCE_TIME, reference_met

_XYERR = {"siguverr": 2.0, "tluverr": 60.0, "zcoruverr": 500.0, "horcoruverr": 40.0}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sim_id(release, variant: str = "hrrr") -> tuple[str, str]:
    return (release.id, variant)


# ---------------------------------------------------------------------------
# Point release - trajectory only
# ---------------------------------------------------------------------------


@integration
def test_particles(tmp_path, wbb_release, traj_only_config):
    """HYSPLIT runs and produces a non-empty trajectory parquet."""
    model = Project.init(
        tmp_path / "trajectory",
        config=traj_only_config,
        releases=[wbb_release],
    )
    model.run()

    sid = _sim_id(wbb_release)
    assert list(model.simulations["release"]) == [sid[0]]
    sim = model.simulation(*sid)
    assert sim.has_particles
    assert sim.particles_path is not None
    assert sim.particles_path.parent.name == "date=2021-01-15"
    assert sim.particles_path.parent.parent.name.startswith("settings=hrrr-")
    assert len(pd.read_parquet(sim.particles_path)) > 0, "Particle file is empty"
    assert sim.particles is not None and len(sim.particles) > 0
    assert not (resolve_compute_root(model) / sid[0] / sid[1]).exists(), (
        "the scratch working directory is removed"
    )

    log_text = sim.log
    for phrase in ("FATAL ERROR", "Segmentation fault", "hycs_std: not found"):
        assert phrase not in log_text, f"Fatal phrase in log: {phrase!r}"


# ---------------------------------------------------------------------------
# Failure path - missing met files
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Idempotency - skip_existing=True
# ---------------------------------------------------------------------------


@integration
def test_idempotency(tmp_path, wbb_release, traj_only_config):
    """Second run with skip_existing=True does not overwrite existing output."""
    model = Project.init(
        tmp_path / "idempotency",
        config=traj_only_config,
        releases=[wbb_release],
    )

    model.run()
    sim = model.simulation(*_sim_id(wbb_release))
    assert sim.has_particles
    mtime_before = sim.particles_path.stat().st_mtime

    model.run()  # skip_existing=True is the default
    assert sim.particles_path.stat().st_mtime == mtime_before, (
        "Parquet was overwritten on second run"
    )


# ---------------------------------------------------------------------------
# Column and multipoint releases
# ---------------------------------------------------------------------------


def _forty_points() -> MultiPointRelease:
    """Forty locations 0.03 degrees apart, 4 m up: HYSPLIT's maxpar cut left the last one empty."""
    lons = -112.4 + 0.03 * np.arange(40)
    lats = 40.76 + 0.0001 * np.arange(40)
    return MultiPointRelease(
        time=REFERENCE_TIME, longitudes=lons, latitudes=lats, altitudes=[4.0] * 40
    )


@integration
def test_every_multipoint_location_gets_the_same_particles(tmp_path, met_dir):
    """With maxpar unset, 1000 particles over 40 locations give each 26; none is cut."""
    import runtraj

    release = _forty_points()
    particles = runtraj.run_trajectories(
        release, reference_met(met_dir), workdir=tmp_path, n_hours=-1,
        numpar=1000, krand=2, seed=42,
    )  # fmt: skip

    # A particle's first row is a minute after release, much nearer its own
    # location than the next one (2.5 km away).
    first = particles.loc[particles.groupby("particle")["age"].idxmax()]
    lons, lats = np.array(release.longitudes), np.array(release.latitudes)
    nearest = np.argmin(
        (first["lon"].to_numpy()[:, None] - lons) ** 2
        + (first["lat"].to_numpy()[:, None] - lats) ** 2,
        axis=1,
    )
    counts = np.bincount(nearest, minlength=40)
    assert counts.tolist() == [26] * 40
    warning = tmp_path / "WARNING"  # HYSPLIT writes it only with a warning
    assert not warning.exists() or "exceeding puff limit" not in warning.read_text()


@integration
def test_a_maxpar_that_cuts_a_multipoint_release_short_fails(tmp_path, met_dir):
    import runtraj

    with pytest.raises(SimulationError, match="40 release locations") as caught:
        runtraj.run_trajectories(
            _forty_points(), reference_met(met_dir), workdir=tmp_path, n_hours=-1,
            numpar=1000, maxpar=1000, krand=2, seed=42,
        )  # fmt: skip
    assert caught.value.reason == FailureReason.PARTICLE_LIMIT


# ---------------------------------------------------------------------------
# CLI - runtraj run via CliRunner
# ---------------------------------------------------------------------------


@integration
def test_cli_tasks_split_the_releases(tmp_path, traj_only_config):
    """Two `runtraj run --task` shares run every release once; a rerun has nothing to do."""
    from typer.testing import CliRunner

    from runtraj.cli import app

    project_dir = tmp_path / "tasks"
    project_dir.mkdir()
    traj_only_config.to_yaml(project_dir / "config.yaml")
    (project_dir / "releases.csv").write_text(
        "time,lati,long,zagl\n"
        "2021-01-15 06:00:00,40.5,-112.0,5.0\n"
        "2021-01-15 06:00:00,40.6,-111.9,5.0\n"
        "2021-01-15 06:00:00,40.7,-111.8,5.0\n"
    )
    project = Project(project_dir)
    every = list(dict.fromkeys(project.simulations["release"]))

    first = CliRunner().invoke(app, ["run", str(project_dir), "--task", "0/2"])
    assert first.exit_code == 0, first.output
    status = project.status()
    assert set(status.loc[status.state == "complete", "release"]) == {
        every[0],
        every[2],
    }

    second = CliRunner().invoke(app, ["run", str(project_dir), "--task", "1/2"])
    assert second.exit_code == 0, second.output
    assert set(project.status()["state"]) == {"complete"}

    again = CliRunner().invoke(app, ["run", str(project_dir), "--task", "0/2"])
    assert again.exit_code == 0, again.output
    assert "This run:  total=0" in again.output


# ---------------------------------------------------------------------------
# Wind-error variants
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Forward run (n_hours > 0)
# ---------------------------------------------------------------------------


@integration
def test_delt_has_no_effect(tmp_path, wbb_release, met_dir):
    """HYSPLIT steps 1 minute with ICHEM=8 whatever delt is."""
    import runtraj

    def run(name, **settings):
        return runtraj.run_trajectories(
            wbb_release, reference_met(met_dir), workdir=tmp_path / name,
            n_hours=-1, numpar=100, krand=2, seed=7, **settings,
        )  # fmt: skip

    pd.testing.assert_frame_equal(run("delt", delt=5.0), run("default"))

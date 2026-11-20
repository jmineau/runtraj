"""
The core on a transport model unlike HYSPLIT: the toy in ``tests/fixtures/models.py``.

Each test runs a part of the core (run, status, check, fetch, stages,
transforms) on the toy, which writes no row at ``age = 0``, numbers its
particles its own way, and has no executable, workdir, log or met files.
A test here fails when the core leans on something only HYSPLIT does.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

import runtraj
from runtraj import ColumnRelease, MultiPointRelease, PointRelease, Project
from runtraj.transforms import (
    FirstOrderLifetime,
    PressureWeighting,
    apply_transforms,
    dry_deposition_survival,
)

from .fixtures.models import PARTICLE_STEP, TOY
from .fixtures.stages import COUNT

NOON = dt.datetime(2023, 1, 1, 12)


def _release(hour: int = 12) -> PointRelease:
    return PointRelease(
        time=NOON + dt.timedelta(hours=hour - 12),
        longitude=-111.85,
        latitude=40.77,
        altitude=5.0,
    )


def _project(tmp_path, releases=None, *, toy=None, mets=None, **settings) -> Project:
    """Make a project whose one model is the toy, and one met that names no files."""
    settings.setdefault("variants", {"toy": {}})
    return Project.init(
        tmp_path / "proj",
        releases=releases or [_release(12), _release(13)],
        mets=mets or {"toy": {"weather": "hrrr"}},
        models={"toy": {"class": TOY, "n_hours": -3, **(toy or {})}},
        **settings,
    )


def _run(project: Project) -> pd.DataFrame:
    return project.run(compute_root=project.directory.parent / "compute")


def _first(project: Project) -> runtraj.Simulation:
    row = project.simulations.iloc[0]
    return project.simulation(row["release"], row["variant"])


def test_a_project_runs_on_the_toy_and_is_complete(tmp_path):
    project = _project(tmp_path)

    assert (project.status()["state"] == "pending").all()
    _run(project)

    assert (project.status()["state"] == "complete").all()
    assert project.incomplete().empty
    # The toy needs no workdir, so none is made in the compute root.
    assert not any((project.directory.parent / "compute").rglob("*"))
    particles = project.particles(project.simulations)
    assert set(particles["particle"]) == {PARTICLE_STEP * k for k in (1, 2, 3, 4)}
    assert (particles["age"] < 0).all()  # no row at the release
    assert "foot" not in particles.columns


def test_a_toy_run_records_no_met_files(tmp_path):
    project = _project(tmp_path)
    _run(project)

    meta = runtraj.particles.particles_metadata(_first(project).particles_path)
    assert meta.met_files == []
    assert meta.settings["model"]["name"] == TOY
    assert meta.settings["model"]["sha256"] is None  # no executable
    assert "label" not in meta.settings  # the toy's unrecorded field


def test_a_changed_toy_setting_is_another_settings_folder(tmp_path):
    project = _project(
        tmp_path,
        [_release(12)],
        variants={"few": {}, "many": {"n_particles": 6}, "noted": {"label": "x"}},
    )
    _run(project)

    folders = {name: v.particles_hash for name, v in project.variants.items()}
    assert folders["few"] != folders["many"]
    assert folders["few"] == folders["noted"]  # an unrecorded field changes nothing
    counts = project.particles(project.simulations).groupby("variant")["particle"]
    assert counts.nunique().to_dict() == {"few": 4, "many": 6, "noted": 4}


def test_toy_realizations_run_with_their_own_seeds(tmp_path):
    project = _project(
        tmp_path,
        [_release(12)],
        toy={"seed": 7},
        variants={"ens": {"realizations": 2}},
    )
    _run(project)

    particles = project.particles(project.simulations)
    assert sorted(particles["realization"].unique()) == [0, 1]
    first, second = (
        g.sort_values(["age", "particle"])["lon"].to_numpy()
        for _, g in particles.groupby("realization")
    )
    assert not (first == second).all()
    # Realization k runs with seed + k, so each repeats a run with that seed.
    for k, lon in enumerate((first, second)):
        alone = runtraj.run_trajectories(
            _release(12), {"weather": "hrrr"}, model=TOY, n_hours=-3, seed=7 + k
        )
        assert lon.tolist() == alone.sort_values(["age", "particle"])["lon"].tolist()


def test_check_and_fetch_find_nothing_for_a_model_without_met_files(tmp_path):
    # A met that HYSPLIT would check and fetch: a folder without the release's
    # files, and a download. Only the model may act on these keys.
    (tmp_path / "met").mkdir()
    met = {"weather": "hrrr", "directory": str(tmp_path / "met"), "download": "hrrr"}
    project = _project(tmp_path, mets={"toy": met})

    assert project.check().empty
    assert project.fetch().empty


def test_a_stage_runs_on_the_toys_particles(tmp_path):
    project = _project(tmp_path, stages={"count": {"class": COUNT}})
    _run(project)

    assert (project.status()["count"]).all()
    sim = _first(project)
    assert sim.path("count").read_text() == str(len(sim.particles))


def test_run_trajectories_runs_the_toy_without_a_workdir(tmp_path):
    particles = runtraj.run_trajectories(
        _release(), {"weather": "hrrr"}, model=TOY, n_hours=2, step=30
    )

    assert sorted(particles["age"].unique()) == [30, 60, 90, 120]
    assert (particles["time"] > pd.Timestamp(NOON)).all()
    with pytest.raises(ValueError, match="numpar"):
        runtraj.run_trajectories(
            _release(), {"weather": "hrrr"}, model=TOY, n_hours=2, numpar=5
        )


def test_transforms_weigh_the_toys_particles(tmp_path):
    particles = runtraj.run_trajectories(
        _release(), {"weather": "hrrr"}, model=TOY, n_hours=-2, step=30
    )

    weighted = apply_transforms(particles, [FirstOrderLifetime(lifetime_hours=1)])

    # The first row is a whole step after the release, and already loses some.
    first = weighted.loc[weighted["age"] == -30, "weight"]
    assert (first < 1).all()
    assert weighted.groupby("particle")["weight"].is_monotonic_decreasing.all()


def test_dry_deposition_runs_on_toy_rows_a_minute_apart():
    particles = runtraj.run_trajectories(
        _release(), {"weather": "hrrr"}, model=TOY, n_hours=-1, step=1
    )

    survived = dry_deposition_survival(particles, velocity=0.01)

    assert ((survived > 0) & (survived <= 1)).all()


# ---------------------------------------------------------------------------
# What the core still takes from HYSPLIT, found by the toy. Each is an
# expected failure that names its issue, and passes once that is fixed.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="#46: the line runtraj writes when a run starts stays as the log",
)
def test_a_toy_run_has_no_log(tmp_path):
    project = _project(tmp_path)
    _run(project)

    with pytest.raises(FileNotFoundError, match="No log"):
        _first(project).log  # noqa: B018


@pytest.mark.xfail(
    strict=True,
    reason="#44: release heights without an age = 0 row assume HYSPLIT's particle numbering",
)
def test_a_toy_column_releases_heights_are_inside_the_column():
    release = ColumnRelease(
        time=NOON, longitude=-111.85, latitude=40.77, bottom=0, top=100
    )
    particles = runtraj.run_trajectories(release, {}, model=TOY, n_hours=-1, seed=1)

    # Four particles, so four slabs of 25 m: each height is a slab's center.
    assert set(particles["release_height"]) <= {12.5, 37.5, 62.5, 87.5}


@pytest.mark.xfail(
    strict=True,
    reason="#44: the repeated-location rule is HYSPLIT's line source, applied to every model",
)
def test_a_toy_multipoint_release_may_repeat_a_location():
    release = MultiPointRelease(
        time=NOON,
        longitudes=[-111.85, -111.85],
        latitudes=[40.77, 40.77],
        altitudes=[5.0, 50.0],
    )
    particles = runtraj.run_trajectories(release, {}, model=TOY, n_hours=-1)

    assert set(particles["release_height"]) == {5.0, 50.0}


def test_dry_deposition_on_hourly_toy_rows_names_no_hysplit_setting():
    particles = runtraj.run_trajectories(
        _release(), {"weather": "hrrr"}, model=TOY, n_hours=-2, step=60
    )

    with pytest.raises(ValueError, match="every one-minute time step") as error:
        dry_deposition_survival(particles, velocity=0.01)
    # HYSPLIT's setting may be named only as HYSPLIT's.
    assert "outdt" not in str(error.value).split("HYSPLIT")[0]


def test_pressure_weighting_without_pres_names_no_hysplit_setting():
    release = ColumnRelease(
        time=NOON, longitude=-111.85, latitude=40.77, bottom=0, top=100
    )
    particles = runtraj.run_trajectories(release, {}, model=TOY, n_hours=-1, seed=1)

    with pytest.raises(ValueError, match="'pres'") as error:
        PressureWeighting().apply(particles)
    assert "varsiwant" not in str(error.value).split("HYSPLIT")[0]

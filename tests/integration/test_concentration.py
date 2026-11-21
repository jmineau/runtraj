"""HYSPLIT's concentration mode, run for real on the test met cache."""

import datetime as dt

import numpy as np
import pytest

from runtraj import PointRelease, Project
from runtraj.models import run_model
from runtraj.models.hysplit import HysplitConfig
from runtraj.models.hysplit.driver import PARTICLE_FILE

from ..conftest import integration
from .conftest import REFERENCE_TIME, reference_met

pytest.importorskip("netCDF4")

#: Two pollutants and two grids, as the reader's fixture files have.
CONCENTRATION = {
    "mode": "concentration",
    "numpar": 500,
    "pollutants": [
        {"name": "TRCA", "rate": 1.0, "hours": 1.0},
        {"name": "TRCB", "rate": 5.0, "hours": 0.5},
    ],
    "grids": [
        {"name": "local", "levels": [0, 100]},
        {
            "name": "wide",
            "center_lon": -112.0,
            "center_lat": 40.6,
            "spacing_lat": 0.1,
            "spacing_lon": 0.1,
            "span_lat": 2.0,
            "span_lon": 2.0,
            "levels": [500],
            "sampling": "snapshot",
            "sampling_interval": 120,
        },
    ],
}


def _forward_release() -> PointRelease:
    return PointRelease(
        time=dt.datetime(2021, 1, 15, 0, 30),
        longitude=-112.0,
        latitude=40.5,
        altitude=10.0,
    )


@integration
def test_a_project_runs_a_concentration_variant_beside_a_particles_one(
    tmp_path, met_dir
):
    release = _forward_release()
    project = Project.init(
        tmp_path / "project",
        mets={"hrrr": reference_met(met_dir)},
        models={"hysplit": {"n_hours": 4, "numpar": 100}},
        variants={"hrrr": {}, "conc": CONCENTRATION},
        releases=[release],
    )
    project.run()

    status = project.status().set_index("variant")
    assert (status["state"] == "complete").all(), status[["state", "reason", "message"]]
    sim = project.simulation(release.id, "conc")
    assert sim.result_path.suffix == ".nc"
    assert sim.result_path.parent.parent.parent.name == "concentration"
    tree = sim.concentration
    assert sorted(tree.children) == ["local", "wide"]
    local = tree["local"].to_dataset()
    assert list(local.data_vars) == ["TRCA", "TRCB"]
    assert local.sizes["time"] == 4  # hourly averages over 4 hours
    assert local["time"].values[0] == np.datetime64("2021-01-15T00:30")
    assert float(local["TRCA"].sel(z=100).max()) > 0
    # Nothing deposits: a gas with no removal.
    assert float(local["TRCA"].sel(z=0).max()) == 0
    wide = tree["wide"].to_dataset()
    assert (wide["time"].values == wide["time_end"].values).all()
    assert float(wide["lat"].mean()) == pytest.approx(40.6, abs=1e-4)
    # The record names the run's met files and its settings.
    assert tree.attrs["release"] == release.id
    assert "TRCA" in tree.attrs["runtraj:settings"]
    # The particles variant is untouched by the other's mode.
    assert len(project.simulation(release.id, "hrrr").particles)


@integration
def test_a_backward_concentration_run_samples_back_in_time(tmp_path, met_dir):
    release = PointRelease(
        time=REFERENCE_TIME, longitude=-112.0, latitude=40.5, altitude=10.0
    )
    config = HysplitConfig(n_hours=-3, **CONCENTRATION)
    run = run_model(
        "hysplit", release, config, reference_met(met_dir), tmp_path / "work"
    )
    local = run.result["local"].to_dataset()
    starts = local["time"].values
    assert (np.diff(starts) < np.timedelta64(0, "s")).all()  # each sample further back
    assert float(local["TRCA"].sel(z=100).max()) > 0
    assert (tmp_path / "work" / "cdump_local").exists()
    assert not (tmp_path / "work" / PARTICLE_FILE).exists()

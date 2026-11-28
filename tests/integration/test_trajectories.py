"""HYSPLIT's trajectory mode, run for real on the test met cache. Needs a ``hyts_std`` on ``PATH``."""

import datetime as dt
import shutil
from pathlib import Path

import pandas as pd
import pytest

import runtraj
from runtraj import MultiPointRelease, PointRelease, Project
from runtraj.exceptions import SimulationError
from runtraj.models import run_model
from runtraj.models.hysplit import HysplitConfig, HysplitModel, read_tdump
from runtraj.models.hysplit.driver import TRAJECTORY_FULL_HOURS, version_at_least

from ..conftest import integration
from .conftest import reference_met

pytestmark = pytest.mark.skipif(
    shutil.which("hyts_std") is None,
    reason="No hyts_std on PATH: the trajectory mode needs a HYSPLIT build that has it.",
)

FIXTURE = Path(__file__).parents[1] / "models" / "hysplit" / "tdump"

#: The release and settings of the reader's fixture, ``tdump/forward``.
STACKED = MultiPointRelease(
    time=dt.datetime(2021, 7, 15, 2, 0),
    longitudes=[-112.0, -112.0, -111.5],
    latitudes=[40.5, 40.5, 41.0],
    altitudes=[10.0, 500.0, 100.0],
)
EVERY_VALUE = {
    "mode": "trajectory",
    "tout": 30,
    **{
        f"tm_{name}": 1
        for name in (
            "tpot",
            "tamb",
            "rain",
            "mixd",
            "relh",
            "sphu",
            "mixr",
            "terr",
            "dswf",
        )
    },
}


@integration
def test_a_project_runs_a_trajectory_variant_beside_a_particles_one(tmp_path, met_dir):
    release = PointRelease(
        time=dt.datetime(2021, 7, 15, 2, 0),
        longitude=-112.0,
        latitude=40.5,
        altitude=10.0,
    )
    project = Project.init(
        tmp_path / "project",
        mets={"hrrr": reference_met(met_dir)},
        models={"hysplit": {"n_hours": 3, "numpar": 100}},
        variants={"hrrr": {}, "traj": {"mode": "trajectory", "tm_tpot": 1}},
        releases=[release, STACKED],
    )
    project.run()

    status = project.status().set_index("variant")
    assert (status.loc["traj", "state"] == "complete").all(), status[
        ["state", "reason", "message"]
    ]
    sim = project.simulation(release.id, "traj")
    assert sim.result_path.parent.parent.parent.name == "trajectories"
    table = sim.trajectories
    assert table["age"].tolist() == [0, 3600, 7200, 10800]
    assert table["time"].iloc[0] == pd.Timestamp(release.time, tz="UTC")
    assert (table["lon"].iloc[0], table["lat"].iloc[0], table["zagl"].iloc[0]) == (
        -112.0,
        40.5,
        10.0,
    )
    assert list(table.columns)[6:] == ["pres", "tpot"]
    assert sim.log  # HYSPLIT's log is kept, as for any run
    # Two points at one place are two trajectories.
    every = project.trajectories()
    assert every.groupby("release")["trajectory"].nunique().to_dict() == {
        release.id: 1,
        STACKED.id: 3,
    }


@integration
def test_a_run_writes_the_reader_fixture_again(tmp_path, met_dir):
    # runtraj's own input files give the trajectories of the fixture, which
    # hand-written files made on NOAA's public 5.4.2.
    config = HysplitConfig(n_hours=6, **EVERY_VALUE)
    if HysplitModel().version(config) != "hysplit.v5.4.2":
        pytest.skip("The fixture is of HYSPLIT 5.4.2.")
    run = run_model("hysplit", STACKED, config, reference_met(met_dir), tmp_path / "w")
    expected = read_tdump(FIXTURE / "forward")
    expected.attrs = {}
    pd.testing.assert_frame_equal(run.result, expected)
    assert not (tmp_path / "w" / "PARTICLE_STILT.DAT").exists()


@integration
def test_a_backward_trajectory_goes_back_in_time(tmp_path, met_dir):
    config = HysplitConfig(n_hours=-3, mode="trajectory")
    if not version_at_least(HysplitModel().version(config), TRAJECTORY_FULL_HOURS):
        pytest.skip("Before 5.4.0 hyts_std runs short from a release off the hour.")
    release = PointRelease(
        time=dt.datetime(2021, 7, 15, 10, 30),
        longitude=-112.0,
        latitude=40.5,
        altitude=10.0,
    )
    # One release without a project, as the guide runs it.
    table = runtraj.run_trajectories(
        release,
        reference_met(met_dir),
        model="hysplit",
        n_hours=-3,
        mode="trajectory",
        tout=30,
    )
    assert table["age"].tolist() == [0, -1800, -3600, -5400, -7200, -9000, -10800]
    assert table["time"].is_monotonic_decreasing


@integration
def test_an_off_hour_release_is_refused_before_hyts_std_5_4_0(tmp_path, met_dir):
    config = HysplitConfig(n_hours=-3, mode="trajectory")
    if version_at_least(HysplitModel().version(config), TRAJECTORY_FULL_HOURS):
        pytest.skip("From 5.4.0 hyts_std runs a release off the hour in full.")
    release = PointRelease(
        time=dt.datetime(2021, 7, 15, 10, 30),
        longitude=-112.0,
        latitude=40.5,
        altitude=10.0,
    )
    with pytest.raises(SimulationError, match="Release on the hour") as raised:
        run_model("hysplit", release, config, reference_met(met_dir))
    assert raised.value.reason == "RELEASE_REFUSED"


@integration
def test_a_location_outside_the_mets_domain_fails_the_run(tmp_path, met_dir):
    release = MultiPointRelease(
        time=dt.datetime(2021, 7, 15, 2, 0),
        longitudes=[-112.0, -112.0],
        latitudes=[40.5, 45.0],
        altitudes=[10.0, 10.0],
    )
    config = HysplitConfig(n_hours=3, mode="trajectory")
    with pytest.raises(SimulationError) as raised:
        run_model("hysplit", release, config, reference_met(met_dir))
    assert raised.value.reason == "MET_COVERAGE"


@integration
def test_met_that_runs_out_fails_a_trajectory_run(tmp_path, met_dir):
    # The met cache ends at 11:00; hyts_std stops there and reports a
    # complete run, with a warning the driver reads.
    release = PointRelease(
        time=dt.datetime(2021, 7, 15, 9, 0),
        longitude=-112.0,
        latitude=40.5,
        altitude=10.0,
    )
    config = HysplitConfig(n_hours=6, mode="trajectory")
    with pytest.raises(SimulationError):
        run_model("hysplit", release, config, reference_met(met_dir))

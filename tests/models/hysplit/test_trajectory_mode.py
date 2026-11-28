"""HYSPLIT's trajectory mode: its settings, its input files, what it records, and what it refuses."""

import datetime as dt
import shutil
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from runtraj import ColumnRelease, MultiPointRelease, PointRelease, Project
from runtraj.exceptions import SimulationError
from runtraj.identity import settings_hash
from runtraj.models import ModelRun, run_model
from runtraj.models.hysplit import (
    ControlFile,
    HysplitConfig,
    HysplitModel,
    read_namelist,
    read_tdump,
    write_inputs,
)
from runtraj.models.hysplit.config import (
    TRAJECTORY_ONLY_SETTINGS,
    TRAJECTORY_SETTINGS,
)
from runtraj.models.hysplit.driver import find_program, setup_entries
from runtraj.models.hysplit.model import _read_trajectories
from tests.fixtures.factories import (
    blocks,
    make_hysplit_dir,
    make_met_config,
    program_path,
)

TDUMP = Path(__file__).parent / "tdump"


def _trajectory(**fields) -> HysplitConfig:
    return HysplitConfig(n_hours=6, mode="trajectory", **fields)


# -- the config ----------------------------------------------------------------


def test_the_model_makes_trajectories_with_hyts_std():
    model = HysplitModel()
    config = _trajectory()
    assert model.result(config) == "trajectories"
    assert model.result_files(config) == ("tdump",)
    assert find_program("hyts_std").stem == "hyts_std"
    # Another program than a particles run's, so another build in the hash.
    assert model.sha256(config) != model.sha256(HysplitConfig(n_hours=6))
    assert model.version(config) == model.version(HysplitConfig(n_hours=6))


def test_a_trajectory_config_gets_the_defaults_of_hyts_std():
    config = _trajectory()
    assert (config.tout, config.kmix0) == (60, 0)
    assert (_trajectory(tout=30, kmix0=150).tout, _trajectory(kmix0=150).kmix0) == (
        30,
        150,
    )
    # A particles config keeps the dispersion model's.
    assert (HysplitConfig(n_hours=6).tout, HysplitConfig(n_hours=6).kmix0) == (0, 150)


def test_a_trajectory_record_holds_the_settings_hyts_std_reads_and_no_other():
    record = _trajectory(numpar=7, varsiwant=["time", "indx"], tm_rain=1).settings()
    assert tuple(record) == TRAJECTORY_SETTINGS
    assert record["mode"] == "trajectory"
    assert record["tm_rain"] == 1
    assert HysplitConfig.model_validate(record).settings() == record


def test_a_setting_of_another_mode_changes_no_trajectory_folder():
    plain = settings_hash(_trajectory().settings())
    assert settings_hash(_trajectory(numpar=7, emisshrs=1.0).settings()) == plain
    assert settings_hash(_trajectory(tout=30).settings()) != plain
    assert settings_hash(HysplitConfig(n_hours=6).settings()) != plain


def test_a_particles_record_names_no_trajectory_setting():
    record = HysplitConfig(n_hours=6).settings()
    assert not set(TRAJECTORY_ONLY_SETTINGS) & set(record)
    assert "mode" not in record


@pytest.mark.parametrize("mode", ["particle", "concentration"])
def test_another_mode_refuses_the_values_along_a_trajectory(mode):
    extra = (
        {"pollutants": [{"name": "TRCA"}], "grids": [{"name": "g"}]}
        if mode == "concentration"
        else {}
    )
    with pytest.raises(ValidationError, match="tm_tpot are settings of HYSPLIT's mode"):
        HysplitConfig(n_hours=6, mode=mode, tm_tpot=1, **extra)


def test_a_trajectory_run_refuses_pollutants():
    with pytest.raises(ValidationError, match="mode is trajectory"):
        _trajectory(pollutants=[{"name": "TRCA"}])


@pytest.mark.parametrize("tout", [1, 5, 12, 30, 60, 90, 180])
def test_an_output_interval_hysplit_keeps_is_accepted(tout):
    assert _trajectory(tout=tout).tout == tout


@pytest.mark.parametrize(("tout", "lowered"), [(0, 1), (7, 6), (45, 30), (59, 30)])
def test_an_output_interval_hysplit_would_lower_is_refused(tout, lowered):
    with pytest.raises(ValidationError, match=rf"lowers it \(to {lowered}\)"):
        _trajectory(tout=tout)


@pytest.mark.parametrize(
    ("setting", "match"),
    [
        ({"kagl": 0}, "above ground"),
        ({"nstr": 6}, "add releases"),
        ({"nver": 2}, "splits"),
    ],
)
def test_what_runtraj_fixes_cannot_be_set(setting, match):
    with pytest.raises(ValidationError, match=match):
        _trajectory(**setting)
    # Its fixed value, or the setting in another mode, is fine.
    assert _trajectory(kagl=1).kagl == 1
    assert HysplitConfig(n_hours=6, **setting)


def test_realizations_are_refused_since_nothing_is_random():
    assert len(_trajectory().realizations(1)) == 1
    with pytest.raises(ValueError, match="draws no random numbers"):
        _trajectory().realizations(3)


def test_a_trajectory_variant_inherits_a_block_set_for_particles(tmp_path):
    from runtraj.config import ProjectConfig

    config = ProjectConfig(
        **blocks(
            mets={"hrrr": make_met_config(tmp_path / "met")},
            n_hours=-6,
            numpar=100,
            varsiwant=["time", "indx", "long", "lati", "zagl", "foot"],
            variants={"particles": {}, "traj": {"mode": "trajectory", "tm_tpot": 1}},
        )
    )
    variants = config.resolve()
    assert variants["particles"].result == "particles"
    assert variants["traj"].result == "trajectories"
    record = variants["traj"].transport.settings()
    assert "numpar" not in record and record["tout"] == 60


# -- the input files -----------------------------------------------------------


def test_setup_holds_only_what_hyts_std_reads():
    entries = setup_entries(_trajectory(numpar=7, tm_tpot=1, tout=30))
    assert entries == {
        "kagl": 1,
        "delt": 0.0,
        "tratio": 0.75,
        "mgmin": 10,
        "khmax": 9999,
        "k10m": 1,
        "kmix0": 0,
        "tout": 30,
        "tm_tpot": 1,
        "tm_tamb": 0,
        "tm_rain": 0,
        "tm_mixd": 0,
        "tm_relh": 0,
        "tm_sphu": 0,
        "tm_mixr": 0,
        "tm_terr": 0,
        "tm_dswf": 0,
        "wbbh": 0.0,
        "wbwf": 0.0,
        "wbwr": 0.0,
    }


def test_a_particles_setup_names_no_trajectory_setting():
    assert not set(TRAJECTORY_ONLY_SETTINGS) & set(
        setup_entries(HysplitConfig(n_hours=6))
    )


def test_control_is_the_file_hyts_std_ran_for_the_fixture(tmp_path):
    # tdump/CONTROL made tdump/forward (tdump/README.md).
    control = ControlFile(
        release_time=dt.datetime(2021, 7, 15, 2, 0),
        locations=[(-112.0, 40.5, 10.0), (-112.0, 40.5, 500.0), (-111.5, 41.0, 100.0)],
        emisshrs=0.01,
        n_hours=6,
        w_option=0,
        z_top=25000.0,
        met_files=[
            Path("<met_cache>/20210715_00-05_hrrr.crop_-114.00_39.00_-110.00_42.00"),
            Path("<met_cache>/20210715_06-11_hrrr.crop_-114.00_39.00_-110.00_42.00"),
        ],
        trajectory=True,
    )
    control.write(tmp_path / "CONTROL")
    assert (tmp_path / "CONTROL").read_text() == (TDUMP / "CONTROL").read_text()
    back = ControlFile.read(tmp_path / "CONTROL")
    assert back.trajectory and back.locations == control.locations
    assert back.n_hours == 6 and len(back.met_files) == 2


def test_inputs_link_hyts_std_and_write_none_of_the_dispersion_files(tmp_path):
    config = _trajectory(ziscale=0.8, tm_terr=1)
    write_inputs(
        tmp_path,
        release_time=dt.datetime(2021, 7, 15, 2, 0),
        locations=[(-112.0, 40.5, 10.0)],
        config=config,
        met_files=[Path("/met/a.arl")],
        kmsl=1,
    )
    names = {p.name for p in tmp_path.iterdir()}
    assert program_path(Path(), "hyts_std").name in names
    assert program_path(Path(), "hycs_std").name not in names
    assert "ZICONTROL" not in names
    setup = {k.lower(): v for k, v in read_namelist(tmp_path / "SETUP.CFG").items()}
    assert setup["kagl"] == 1 and setup["kmsl"] == 1 and setup["tm_terr"] == 1
    assert not {"numpar", "ichem", "idsp", "maxpar", "ivmax"} & set(setup)
    assert (tmp_path / "CONTROL").read_text().endswith("./\ntdump\n")


# -- releases ------------------------------------------------------------------


def _points(*releases) -> pd.DataFrame:
    from runtraj.models.run import _release_points

    return _release_points(releases)


def test_a_column_release_is_refused_and_a_repeated_location_is_not():
    time = dt.datetime(2021, 7, 15, 2, 0)
    column = ColumnRelease(
        time=time, longitude=-112.0, latitude=40.5, bottom=10.0, top=500.0
    )
    stacked = MultiPointRelease(
        time=time,
        longitudes=[-112.0, -112.0, -111.5],
        latitudes=[40.5, 40.5, 41.0],
        altitudes=[10.0, 500.0, 100.0],
    )
    model = HysplitModel()
    refused = model.check_releases(_points(column, stacked), _trajectory())
    assert refused["release"].tolist() == [column.id]
    assert "ColumnRelease" in refused["message"].iloc[0]
    # The dispersion model's rule is the other way around.
    refused = model.check_releases(_points(column, stacked), HysplitConfig(n_hours=6))
    assert refused["release"].tolist() == [stacked.id]


def _point(minute: int) -> PointRelease:
    return PointRelease(
        time=dt.datetime(2021, 7, 15, 10, minute),
        longitude=-112.0,
        latitude=40.5,
        altitude=10.0,
    )


@pytest.mark.parametrize(
    ("version", "refused"),
    [
        ("hysplit.v5.1.0", True),
        ("hysplit.v5.3.4", True),
        (None, True),  # a version runtraj cannot read is taken to be older
        ("hysplit.v5.4.0", False),
        ("hysplit.v5.4.2", False),
    ],
)
def test_an_off_hour_release_is_refused_before_hyts_std_5_4_0(
    tmp_path, version, refused
):
    config = _trajectory(directory=make_hysplit_dir(tmp_path / "hysplit", version))
    found = HysplitModel().check_releases(_points(_point(30), _point(0)), config)
    assert found["release"].tolist() == ([_point(30).id] if refused else [])
    if refused:
        message = found["message"].iloc[0]
        assert "first hour" in message
        assert "Release on the hour, or use HYSPLIT 5.4.0 or later" in message


def test_an_off_hour_release_is_refused_in_a_run_before_hyts_runs(tmp_path):
    ran = tmp_path / "ran"
    # Asked for its version, in an empty folder, hyts_std has no CONTROL.
    body = f"test -f CONTROL && touch {ran}\nexit 2\n"
    build = make_hysplit_dir(tmp_path / "hysplit", "hysplit.v5.3.4", body)
    config = _trajectory(directory=build)
    with pytest.raises(SimulationError, match="Release on the hour") as raised:
        run_model("hysplit", _point(30), config, make_met_config(tmp_path / "met"))
    assert raised.value.reason == "RELEASE_REFUSED"
    assert not ran.exists()


def test_the_particle_mode_takes_a_release_off_the_hour(tmp_path):
    build = make_hysplit_dir(tmp_path / "hysplit", "hysplit.v5.3.4")
    config = HysplitConfig(n_hours=-6, directory=build)
    assert HysplitModel().check_releases(_points(_point(30)), config).empty


def test_an_off_hour_release_is_refused_with_no_hyts_std_to_ask(tmp_path):
    # The model check reports a missing build; this one has nothing to say.
    config = _trajectory(directory=tmp_path / "nothing")
    assert HysplitModel().check_releases(_points(_point(30)), config).empty


def test_a_column_release_fails_before_hyts_std_runs(tmp_path):
    column = ColumnRelease(
        time=dt.datetime(2021, 7, 15, 2, 0),
        longitude=-112.0,
        latitude=40.5,
        bottom=10.0,
        top=500.0,
    )
    with pytest.raises(SimulationError, match="ColumnRelease") as raised:
        run_model("hysplit", column, _trajectory(), make_met_config(tmp_path / "met"))
    assert raised.value.reason == "RELEASE_REFUSED"


# -- what a run wrote ----------------------------------------------------------


def test_a_runs_file_is_read_as_its_trajectories(tmp_path):
    shutil.copy(TDUMP / "forward", tmp_path / "tdump")
    table = _read_trajectories(tmp_path, locations=3)
    assert len(table) == 39 and table.attrs == {}


def test_a_run_without_a_file_or_a_row_fails_with_its_type(tmp_path):
    with pytest.raises(SimulationError, match="wrote no tdump") as raised:
        _read_trajectories(tmp_path, locations=1)
    assert raised.value.reason == "NO_TRAJECTORIES_DATA"
    header = (TDUMP / "backward").read_text().splitlines()[:6]
    (tmp_path / "tdump").write_text("\n".join(header) + "\n")
    with pytest.raises(SimulationError, match="no trajectory") as raised:
        _read_trajectories(tmp_path, locations=1)
    assert raised.value.reason == "NO_TRAJECTORIES_DATA"


def test_a_run_that_left_out_a_location_fails(tmp_path):
    # hyts_std drops a location outside the met's domain and numbers the
    # rest from 1.
    shutil.copy(TDUMP / "backward", tmp_path / "tdump")
    with pytest.raises(SimulationError, match="1 of the release's 2") as raised:
        _read_trajectories(tmp_path, locations=2)
    assert raised.value.reason == "MET_COVERAGE"


# -- a project -----------------------------------------------------------------


def test_a_project_stores_and_reads_trajectories_beside_particles(
    tmp_path, monkeypatch, transport_runs
):
    releases = [
        PointRelease(
            time=dt.datetime(2021, 7, 15, 2 + k, 0),
            longitude=-112.0,
            latitude=40.5,
            altitude=10.0,
        )
        for k in range(2)
    ]

    def run(model, release, config, *args, **kwargs):
        if config.mode != "trajectory":
            return transport_runs(model, release, config, *args, **kwargs)
        return ModelRun(read_tdump(TDUMP / "backward"), log="the run's log\n")

    monkeypatch.setattr("runtraj.execution.worker.run_model", run)
    project = Project.init(
        tmp_path / "project",
        mets={"hrrr": make_met_config(tmp_path / "met")},
        models={"hysplit": {"n_hours": -6}},
        variants={"hrrr": {}, "traj": {"mode": "trajectory"}},
        releases=releases,
    )
    project.run()

    status = project.status().set_index("variant")
    assert (status["state"] == "complete").all(), status[["state", "message"]]
    assert status.loc["traj", "trajectories"].all()
    assert status.loc["hrrr", "trajectories"].isna().all()
    assert project.output.kinds() == ["particles", "trajectories"]

    sim = project.simulation(releases[0].id, "traj")
    assert sim.result_path.suffix == ".parquet"
    assert sim.result_path.parent.parent.parent.name == "trajectories"
    assert sim.trajectories["age"].tolist() == [0, -3600, -7200, -10800, -14400]

    table = project.trajectories()
    assert list(table.columns[:2]) == ["release", "variant"]
    assert table.groupby("release").size().to_dict() == {r.id: 5 for r in releases}
    assert (table["variant"] == "traj").all()
    one = project.trajectories(project.simulations.query("variant == 'traj'").head(1))
    assert one["release"].unique().tolist() == [releases[0].id]

    # Each verb reads its own type.
    with pytest.raises(ValueError, match="make no trajectories"):
        project.trajectories(project.simulations)
    with pytest.raises(ValueError, match="project.trajectories"):
        project.particles(project.simulations)
    with pytest.raises(FileNotFoundError, match="makes particles"):
        project.simulation(releases[0].id, "hrrr").trajectories  # noqa: B018
    assert len(project.particles())


def test_a_stage_on_a_trajectory_variant_is_refused(tmp_path):
    from tests.fixtures.stages import COUNT

    with pytest.raises(ValueError, match="count reads particles"):
        Project.init(
            tmp_path / "project",
            mets={"hrrr": make_met_config(tmp_path / "met")},
            models={"hysplit": {"n_hours": -6}},
            stages={"count": {"class": COUNT}},
            variants={"traj": {"mode": "trajectory"}},
        )

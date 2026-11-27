"""HYSPLIT's concentration mode: its settings, its input files, and what it records."""

import datetime as dt
from pathlib import Path

import pytest
from pydantic import ValidationError

from runtraj.identity import run_settings, settings_hash
from runtraj.models import ModelInfo
from runtraj.models.hysplit import (
    ConcentrationGrid,
    ControlFile,
    HysplitConfig,
    HysplitModel,
    Pollutant,
)
from runtraj.models.hysplit.control import cdump_name
from runtraj.models.hysplit.driver import setup_entries
from runtraj.testing import check_model

GRID = {"name": "local", "spacing_lat": 0.05, "spacing_lon": 0.05, "levels": [0, 100]}


def _concentration(**fields) -> HysplitConfig:
    return HysplitConfig(
        n_hours=4,
        mode="concentration",
        pollutants=[{"name": "TRCA"}],
        grids=[GRID],
        **fields,
    )


def test_a_particles_config_hashes_as_it_did_before_the_mode():
    # Hashed on main before HYSPLIT had a mode: every stored particles
    # folder must keep its hash.
    config = HysplitConfig(n_hours=-24, numpar=500, seed=3, krand=2, cpack=1)
    record = config.settings()
    assert not {"mode", "pollutants", "grids"} & set(record)
    assert settings_hash(record) == (
        "72fd5770febce5bc48c14b37c2db442ab756841cff04a8813863604932d433c3"
    )
    run = run_settings(
        config,
        {"product": "hrrr"},
        ModelInfo(name="hysplit", sha256="0" * 64),
        ensemble=False,
    )
    assert settings_hash(run) == (
        "e9c581e2c19b42d842e13d5e2e0cefee43caea19b17a4bf087f55623d9b28fe5"
    )


def test_the_model_makes_the_modes_result():
    assert HysplitModel().result(HysplitConfig(n_hours=-24)) == "particles"
    assert HysplitModel().result(_concentration()) == "concentration"


def test_a_failed_runs_kept_workdir_leaves_out_the_files_of_its_result():
    assert HysplitModel().result_files(_concentration()) == ("cdump_local",)
    assert HysplitModel().result_files(HysplitConfig(n_hours=-24)) == (
        "PARTICLE_STILT.DAT",
    )


def test_a_concentration_record_reads_back_and_leaves_out_the_particle_settings():
    record = _concentration().settings()
    assert record["mode"] == "concentration"
    assert record["grids"][0]["name"] == "local"
    assert not {"varsiwant", "outdt", "veght", "emisshrs"} & set(record)
    # check_model reads the record back to the same record.
    check_model(HysplitModel(), _concentration())


def test_two_modes_never_share_a_settings_folder():
    particles = HysplitConfig(n_hours=4)
    assert settings_hash(particles.settings()) != settings_hash(
        _concentration().settings()
    )


@pytest.mark.parametrize("missing", ["pollutants", "grids"])
def test_a_concentration_run_needs_a_pollutant_and_a_grid(missing):
    fields = {"pollutants": [{"name": "TRCA"}], "grids": [GRID]}
    del fields[missing]
    with pytest.raises(ValidationError, match=f"at least one of {missing}"):
        HysplitConfig(n_hours=4, mode="concentration", **fields)


@pytest.mark.parametrize(
    "setting",
    [{"varsiwant": ["time", "indx"]}, {"outdt": 5}, {"veght": 0.3}, {"emisshrs": 1}],
)
def test_a_concentration_run_leaves_out_the_particle_files_settings(setting):
    # A hysplit block may set them for its particle variants.
    config = _concentration(**setting)
    assert config.settings() == _concentration().settings()
    entries = setup_entries(config)
    assert not {"varsiwant", "outdt", "veght"} & set(entries)


def test_a_particles_run_refuses_pollutants_and_grids():
    with pytest.raises(ValidationError, match="mode: concentration"):
        HysplitConfig(n_hours=4, grids=[GRID])


def test_names_are_one_each():
    with pytest.raises(ValidationError, match="named"):
        HysplitConfig(
            n_hours=4,
            mode="concentration",
            pollutants=[{"name": "TRCA"}, {"name": "TRCA"}],
            grids=[GRID],
        )


def test_a_one_point_file_is_refused_before_the_run():
    with pytest.raises(ValidationError, match="cpack=2"):
        _concentration(cpack=2)


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"center_lon": -111.9}, "together"),
        ({"center_lon": 0.0, "center_lat": 0.0}, "release location"),
        ({"levels": [100, 0]}, "each above"),
        ({"levels": [0, 0]}, "each above"),
        ({"levels": [-10]}, "0 m or more"),
    ],
)
def test_a_grid_is_checked(fields, match):
    with pytest.raises(ValidationError, match=match):
        ConcentrationGrid(name="g", **fields)


@pytest.mark.parametrize("minutes", [1, 25, 59, 60, 120, 360])
def test_an_interval_hysplit_keeps_is_accepted(minutes):
    assert ConcentrationGrid(name="g", sampling_interval=minutes)


@pytest.mark.parametrize(
    ("minutes", "nearest"), [(61, "60 or 120"), (90, "60 or 120"), (150, "120 or 180")]
)
def test_an_interval_hysplit_would_shorten_is_refused(minutes, nearest):
    # Under an hour HYSPLIT keeps the minutes; from an hour up it drops them,
    # so 90 gives the samples of 60 (conset.f), not 90 minutes.
    with pytest.raises(ValidationError, match=f"whole hours.*Use {nearest}"):
        ConcentrationGrid(name="g", sampling_interval=minutes)


def test_a_pollutant_name_is_hysplits_four_characters():
    with pytest.raises(ValidationError):
        Pollutant(name="TOOLONG")


@pytest.mark.parametrize("name", ["lat", "lon", "z", "time"])
def test_a_pollutant_cannot_be_named_like_a_coordinate(name):
    # A grid's variable and its coordinate cannot share a name in one Dataset.
    with pytest.raises(ValidationError, match="name of a coordinate"):
        Pollutant(name=name)


def test_setup_writes_hysplits_concentration_output_and_no_particle_file():
    entries = setup_entries(_concentration())
    assert entries["ichem"] == 0
    assert "idsp" not in entries  # HYSPLIT's own dispersion
    assert not {"varsiwant", "outdt", "veght", "mode", "grids", "pollutants"} & set(
        entries
    )
    particles = setup_entries(HysplitConfig(n_hours=4))
    assert (particles["ichem"], particles["idsp"]) == (8, 2)


def _control(**fields) -> ControlFile:
    return ControlFile(
        release_time=dt.datetime(2021, 7, 15, 1, 30),
        locations=[(-111.848, 40.766, 10.0)],
        emisshrs=0.01,
        n_hours=4,
        w_option=0,
        z_top=10000.0,
        met_files=[Path("/met/a"), Path("/met/b")],
        **fields,
    )


#: What runtraj writes for the two pollutants and two grids of the files in
#: ``cdump/``, whose ``CONTROL`` (hand-written, which hycs_std ran) has the
#: same values.
CONCENTRATION_CONTROL = """\
21 07 15 01 30
1
40.766 -111.848 10.0
4
0
10000.0
1 2
/met/
a
/met/
b
2
TRCA
1.0
1.0
00 00 00 00 00
TRCB
5.0
0.5
00 00 00 00 00
2
0.0 0.0
0.05 0.05
1.0 1.0
./
cdump_local
2
0 100
00 00 00 00 00
00 00 00 00 00
00 01 00
40.7 -111.9
0.1 0.1
2.0 2.0
./
cdump_wide
1
500
00 00 00 00 00
00 00 00 00 00
01 02 00
2
0.0 0.0 0.0
0.0 0.0 0.0 0.0 0.0
0.0 0.0 0.0
0.0
0.0
0.0 0.0 0.0
0.0 0.0 0.0 0.0 0.0
0.0 0.0 0.0
0.0
0.0
"""

POLLUTANTS = [
    Pollutant(name="TRCA", rate=1.0, hours=1.0),
    Pollutant(name="TRCB", rate=5.0, hours=0.5),
]
GRIDS = [
    ConcentrationGrid(name="local", levels=[0, 100]),
    ConcentrationGrid(
        name="wide",
        center_lon=-111.9,
        center_lat=40.7,
        spacing_lat=0.1,
        spacing_lon=0.1,
        span_lat=2.0,
        span_lon=2.0,
        levels=[500],
        sampling="snapshot",
        sampling_interval=120,
    ),
]


def test_control_writes_the_pollutants_grids_and_deposition(tmp_path):
    path = tmp_path / "CONTROL"
    _control(pollutants=POLLUTANTS, grids=GRIDS).write(path)
    assert path.read_text() == CONCENTRATION_CONTROL
    # The lines after the met files are those of the CONTROL hycs_std ran.
    ran = (Path(__file__).parent / "cdump" / "CONTROL").read_text().splitlines()
    ours = path.read_text().splitlines()
    assert [
        line.replace("cdump_local", "cdump1").replace("cdump_wide", "cdump2")
        for line in ours[11:]
    ] == ran[11:]


def test_control_reads_back_its_pollutants_and_grids(tmp_path):
    path = tmp_path / "CONTROL"
    _control(pollutants=POLLUTANTS, grids=GRIDS).write(path)
    loaded = ControlFile.read(path)
    assert loaded.pollutants == POLLUTANTS
    assert loaded.grids == GRIDS


def test_a_control_file_is_read_as_hysplit_reads_its_interval(tmp_path):
    # HYSPLIT drops the minutes of an interval of an hour or more (conset.f), so
    # a file that says 1 h 30 min is a file of hourly samples.
    assert CONCENTRATION_CONTROL.count("00 01 00\n") == 1
    path = tmp_path / "CONTROL"
    path.write_text(CONCENTRATION_CONTROL.replace("00 01 00\n", "00 01 30\n"))
    assert ControlFile.read(path).grids == GRIDS
    path.write_text(CONCENTRATION_CONTROL.replace("00 01 00\n", "00 00 45\n"))
    assert ControlFile.read(path).grids[0].sampling_interval == 45


def test_a_particles_control_reads_back_with_no_pollutants(tmp_path):
    path = tmp_path / "CONTROL"
    _control().write(path)
    loaded = ControlFile.read(path)
    assert loaded.pollutants == [] and loaded.grids == []
    assert "cdump\n" in path.read_text()


def test_each_grid_has_its_own_file():
    assert cdump_name("local") == "cdump_local"


def test_a_concentration_variant_inherits_a_block_set_for_particles(tmp_path):
    from runtraj.config import ProjectConfig
    from tests.fixtures.factories import blocks, make_met_config

    config = ProjectConfig(
        **blocks(
            mets={"hrrr": make_met_config(tmp_path / "met")},
            n_hours=4,
            varsiwant=["time", "indx", "long", "lati", "zagl", "foot"],
            outdt=5,
            variants={
                "particles": {},
                "conc": {
                    "mode": "concentration",
                    "pollutants": [{"name": "TRCA"}],
                    "grids": [GRID],
                },
            },
        )
    )
    variants = config.resolve()
    assert variants["particles"].result == "particles"
    assert variants["conc"].result == "concentration"
    assert "varsiwant" not in variants["conc"].transport.settings()


def test_a_concentration_run_gets_particles_for_every_step_and_cycle():
    # hycs_std 5.4.2 released 2010 particles for numpar 2000 from one
    # location over an hour, and 2070 from three; with MAXPAR at numpar it
    # stops before the end of the emission, and before a second cycle.
    one = _concentration(numpar=2000)
    assert one.maxpar_for(1) >= 2010
    assert one.maxpar_for(3) >= 2070
    assert _concentration(numpar=2000, qcycle=1.0).maxpar_for(1) == 4 * one.maxpar_for(
        1
    )
    assert _concentration(numpar=2000, maxpar=5000).maxpar_for(3) == 5000
    assert one.settings()["maxpar"] is None

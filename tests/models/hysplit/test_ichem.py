"""HYSPLIT's ``ichem`` in the concentration mode: which values run, what each needs, and how its file reads (``cdump/README.md``)."""

import datetime as dt
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from pydantic import ValidationError

from runtraj.concentration import CONCENTRATION_DIMS, check_concentration
from runtraj.identity import settings_hash
from runtraj.models.hysplit import (
    Conversion,
    HysplitConfig,
    HysplitModel,
    read_cdump,
    write_inputs,
)
from runtraj.models.hysplit.config import CONCENTRATION_ICHEM, ICHEM_REFUSED
from runtraj.models.hysplit.control import cdump_name
from runtraj.models.hysplit.driver import chemrate, setup_entries
from runtraj.models.hysplit.model import _read_grids
from runtraj.releases import ColumnRelease, MultiPointRelease
from runtraj.testing import check_model

HERE = Path(__file__).parent / "cdump"
GRID = {"name": "g", "levels": [0, 100]}
TWO = [{"name": "SO2"}, {"name": "SO4", "rate": 0.0}]
SO2_TO_SO4 = {"source": "SO2", "product": "SO4", "rate": 0.1, "weight_ratio": 1.5}


def _concentration(**fields) -> HysplitConfig:
    fields.setdefault("pollutants", [{"name": "TRCA"}])
    fields.setdefault("grids", [GRID])
    return HysplitConfig(n_hours=4, mode="concentration", **fields)


# -- the setting, by mode --------------------------------------------------------


def test_a_concentration_run_records_and_writes_ichem_0_unless_set():
    config = _concentration()
    assert config.ichem == 0
    assert config.settings()["ichem"] == 0
    assert setup_entries(config)["ichem"] == 0
    assert setup_entries(_concentration(ichem=6))["ichem"] == 6
    assert settings_hash(config.settings()) != settings_hash(
        _concentration(ichem=6).settings()
    )
    # check_model reads the record back to the same record.
    check_model(HysplitModel(), _concentration(ichem=6))


def test_the_particle_mode_takes_its_own_ichem_and_records_none():
    plain = HysplitConfig(n_hours=-24)
    with_8 = HysplitConfig(n_hours=-24, ichem=8)
    assert "ichem" not in with_8.settings()
    assert with_8.settings() == plain.settings()
    assert setup_entries(with_8)["ichem"] == 8


@pytest.mark.parametrize("ichem", [0, 6, 10])
def test_the_particle_mode_refuses_another_ichem(ichem):
    with pytest.raises(ValidationError, match="mode: concentration"):
        HysplitConfig(n_hours=-24, ichem=ichem)


def test_a_trajectory_run_neither_writes_nor_records_ichem():
    # hyts_std reads ICHEM, and it changes a trajectory; a block may set it
    # for its other variants.
    config = HysplitConfig(n_hours=-24, mode="trajectory", ichem=8)
    assert "ichem" not in setup_entries(config)
    assert config.settings() == HysplitConfig(n_hours=-24, mode="trajectory").settings()


@pytest.mark.parametrize("ichem", sorted(ICHEM_REFUSED))
def test_a_value_runtraj_does_not_run_is_refused_with_the_reason(ichem):
    with pytest.raises(ValidationError, match=f"ichem={ichem} is not run"):
        _concentration(ichem=ichem)


def test_ichem_8_in_a_concentration_run_names_the_particle_mode():
    with pytest.raises(ValidationError, match="mode: particle"):
        _concentration(ichem=8)


def test_a_value_hysplit_does_not_have_is_refused():
    # hycs_std 5.4.2 runs ICHEM=12 as 0, without a word.
    with pytest.raises(ValidationError, match="not one of HYSPLIT's"):
        _concentration(ichem=12)
    assert set(CONCENTRATION_ICHEM) | set(ICHEM_REFUSED) == set(range(12))


# -- what each value needs -------------------------------------------------------


@pytest.mark.parametrize("ichem", [1, 10])
def test_a_matrix_takes_one_pollutant(ichem):
    # hycs_std 5.4.2 writes the same file for one pollutant and for two.
    with pytest.raises(ValidationError, match="takes one pollutant, not 2"):
        _concentration(ichem=ichem, qcycle=1.0, pollutants=TWO)


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"ichem": 1, "nbptyp": 2}, "nbptyp: 1"),
        ({"ichem": 1, "maxdim": 2}, "maxdim: 1"),
        ({"ichem": 5, "maxdim": 2}, "maxdim: 1"),
        # HYSPLIT changes initd to 0 itself: the run would not be what was set.
        ({"ichem": 5, "initd": 4}, "initd: 0"),
        ({"ichem": 6, "conversions": [SO2_TO_SO4]}, "only with ichem: 2"),
    ],
)
def test_a_setting_hysplit_would_stop_on_or_change_is_refused(fields, match):
    with pytest.raises(ValidationError, match=match):
        _concentration(**fields)


def test_conversions_belong_to_the_concentration_mode():
    with pytest.raises(ValidationError, match="mode: concentration"):
        HysplitConfig(n_hours=4, conversions=[SO2_TO_SO4])


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        # hycs_std 5.4.2 exits 0 and leaves a file with no sample.
        ({"maxdim": 1}, "maxdim set to their number"),
        ({"maxdim": 2, "pollutants": [{"name": "SO2"}]}, "two pollutants or more"),
        ({"maxdim": 2, "conversions": []}, "at least one of conversions"),
        (
            {"maxdim": 2, "conversions": [{**SO2_TO_SO4, "product": "NO3"}]},
            "not one of pollutants",
        ),
        ({"maxdim": 2, "nbptyp": 2}, "nbptyp: 1"),
    ],
)
def test_conversions_are_checked(fields, match):
    fields = {"pollutants": TWO, "conversions": [SO2_TO_SO4], **fields}
    with pytest.raises(ValidationError, match=match):
        _concentration(ichem=2, **fields)


def test_a_pollutant_does_not_convert_into_itself():
    with pytest.raises(ValidationError, match="two pollutants"):
        Conversion(source="SO2", product="SO2", rate=0.1)


def test_conversions_are_written_as_hysplit_reads_them(tmp_path):
    config = _concentration(
        ichem=2,
        maxdim=2,
        pollutants=TWO,
        conversions=[SO2_TO_SO4, {"source": "SO4", "product": "SO2", "rate": 0.01}],
    )
    assert chemrate(config) == ["1 2 0.1 1.5", "2 1 0.01 1.0"]
    assert chemrate(_concentration()) is None
    assert config.settings()["conversions"][0]["weight_ratio"] == 1.5
    check_model(HysplitModel(), config)


def test_a_run_with_conversions_gets_its_chemrate_file(tmp_path, monkeypatch):
    program = tmp_path / "hycs_std"
    program.write_text("")
    monkeypatch.setattr(
        "runtraj.models.hysplit.driver.find_program", lambda name, directory: program
    )
    monkeypatch.setattr(
        "runtraj.models.hysplit.driver.data_tables", lambda directory, program: {}
    )
    args = (dt.datetime(2021, 7, 15, 1, 30), [(-111.848, 40.766, 10.0)])
    config = _concentration(ichem=2, maxdim=2, pollutants=TWO, conversions=[SO2_TO_SO4])
    write_inputs(tmp_path / "with", *args, config, [Path("/met/a")])
    assert (tmp_path / "with" / "CHEMRATE.TXT").read_text() == "1 2 0.1 1.5\n"
    write_inputs(tmp_path / "without", *args, _concentration(), [Path("/met/a")])
    assert not (tmp_path / "without" / "CHEMRATE.TXT").exists()


@pytest.mark.parametrize(
    ("levels", "ok"),
    [([50, 1000], True), ([100], True), ([0, 50], False), ([150, 1000], False)],
)
def test_a_mixed_layer_level_is_a_percentage_and_has_no_deposition_level(levels, ok):
    # With a level of 0 first, hycs_std 5.4.2 turns it into a 10 m level.
    grid = {"name": "g", "levels": levels}
    if ok:
        _concentration(ichem=9, grids=[grid])
        return
    with pytest.raises(ValidationError, match="percentage of the mixed layer"):
        _concentration(ichem=9, grids=[grid])


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({}, "whole number of hours"),  # qcycle 0: HYSPLIT stops
        # Half-hour cycles: 5.4.2 released 6 of 8.
        ({"qcycle": 0.5}, "whole number of hours"),
        # 4 hours of 3-hour cycles: 5.4.2 made one cycle and no second.
        ({"qcycle": 3.0}, "multiple of qcycle"),
        ({"qcycle": 1.0, "pollutants": [{"name": "TRCA", "hours": 2}]}, "longer than"),
        ({"qcycle": 1.0, "efile": "EMITIMES"}, "efile"),
    ],
)
def test_emission_cycles_hysplit_would_drop_are_refused(fields, match):
    with pytest.raises(ValidationError, match=match):
        _concentration(ichem=10, **fields)


def test_a_backward_run_has_no_emission_cycles():
    # 5.4.2 released the first cycle only, and left the others zero.
    with pytest.raises(ValidationError, match="forward run"):
        HysplitConfig(
            n_hours=-4,
            mode="concentration",
            ichem=10,
            qcycle=1.0,
            pollutants=[{"name": "TRCA"}],
            grids=[GRID],
        )


def test_whole_cycles_are_accepted():
    for qcycle in (1.0, 2.0, 4.0):
        _concentration(ichem=10, qcycle=qcycle)


# -- releases --------------------------------------------------------------------


def test_a_column_release_is_no_two_sources():
    from runtraj.models.run import _release_points

    time = dt.datetime(2021, 7, 15, 1, 30)
    column = ColumnRelease(
        time=time, longitude=-111.848, latitude=40.766, bottom=10.0, top=100.0
    )
    points = MultiPointRelease(
        time=time,
        longitudes=[-111.848, -112.0],
        latitudes=[40.766, 40.5],
        altitudes=[10.0, 10.0],
    )
    rows = _release_points([column, points])
    model = HysplitModel()
    # 5.4.2 left the first location's grid empty and put the column in the second.
    refused = model.check_releases(rows, _concentration(ichem=1))
    assert refused["release"].tolist() == [column.id]
    assert "ichem: 1" in refused["message"].iloc[0]
    assert model.check_releases(rows, _concentration()).empty


# -- the files -------------------------------------------------------------------


def test_each_release_location_is_a_source():
    ds = read_cdump(HERE / "ichem1_3loc", ichem=1, pollutant="TRCA")
    assert list(ds.data_vars) == ["TRCA"]
    assert ds["TRCA"].dims == ("source", *CONCENTRATION_DIMS)
    assert ds["source"].values.tolist() == [1, 2, 3]
    np.testing.assert_allclose(ds["source_lon"], [-111.848, -112.0, -111.6], atol=1e-5)
    np.testing.assert_allclose(ds["source_lat"], [40.766, 40.5, 40.9], atol=1e-5)
    assert ds["source_height"].values.tolist() == [10.0, 10.0, 50.0]
    assert all(float(ds["TRCA"].sel(source=k).sum()) > 0 for k in (1, 2, 3))
    # The same run with ichem 0 counts the three together.
    together = read_cdump(HERE / "ichem0_3loc")["TRCA"]
    np.testing.assert_allclose(
        ds["TRCA"].sum("source"), together, rtol=1e-5, atol=1e-18
    )
    # Each source's plume starts in its own cell.
    first = ds["TRCA"].isel(time=0).sel(z=100)
    for k in (1, 2, 3):
        peak = first.sel(source=k)
        at = peak.where(peak == peak.max(), drop=True)
        assert abs(float(at["lat"][0]) - float(ds["source_lat"].sel(source=k))) < 0.3
        assert abs(float(at["lon"][0]) - float(ds["source_lon"].sel(source=k))) < 0.3


def test_each_emission_cycle_is_a_release_time():
    ds = read_cdump(HERE / "ichem10_qcycle1", ichem=10, pollutant="TRCA")
    assert ds["TRCA"].dims == ("release_time", *CONCENTRATION_DIMS)
    start = pd.Timestamp("2021-07-15 01:30")
    expected = [start + pd.Timedelta(hours=k) for k in range(4)]
    assert pd.DatetimeIndex(ds["release_time"].values).tolist() == expected
    # Nothing is counted before it is released.
    per_cycle = ds["TRCA"].sum(("z", "lat", "lon"))
    for k in range(4):
        assert float(per_cycle.isel(release_time=k, time=slice(0, k)).sum()) == 0
        assert float(per_cycle.isel(release_time=k, time=k)) > 0
    # The same emissions with ichem 0 are counted together.
    together = read_cdump(HERE / "ichem0_qcycle1")["TRCA"]
    np.testing.assert_allclose(
        ds["TRCA"].sum("release_time"), together, rtol=1e-4, atol=1e-18
    )


@pytest.mark.parametrize(
    ("file", "ichem"), [("ichem0", 1), ("ichem0", 10), ("ichem1_3loc", 10)]
)
def test_a_file_that_is_no_matrix_is_refused(file, ichem):
    with pytest.raises(ValueError, match=f"not the file of a run with ichem={ichem}"):
        read_cdump(HERE / file, ichem=ichem, pollutant="TRCA")


def test_a_matrix_needs_its_pollutants_name():
    with pytest.raises(ValueError, match="pollutant's name"):
        read_cdump(HERE / "ichem1_3loc", ichem=1)


def test_a_matrix_is_a_concentration_result_and_is_stored(tmp_path):
    from runtraj.concentration import read_concentration, write_concentration
    from runtraj.releases import PointRelease

    pytest.importorskip("netCDF4")
    tree = xr.DataTree.from_dict(
        {
            "sources": read_cdump(HERE / "ichem1_3loc", ichem=1, pollutant="TRCA"),
            "cycles": read_cdump(HERE / "ichem10_qcycle1", ichem=10, pollutant="TRCA"),
        }
    )
    check_concentration(tree)
    release = PointRelease(
        time=dt.datetime(2021, 7, 15, 1, 30),
        longitude=-111.848,
        latitude=40.766,
        altitude=10.0,
    )
    path = write_concentration(tmp_path / "r.nc", tree, release, {}, [])
    back = read_concentration(path)
    xr.testing.assert_identical(
        back["sources"].to_dataset()["TRCA"], tree["sources"].to_dataset()["TRCA"]
    )
    assert (
        back["cycles"]["release_time"].values == tree["cycles"]["release_time"].values
    ).all()


def test_a_grid_takes_one_split_dimension_only():
    ds = read_cdump(HERE / "ichem1_3loc", ichem=1, pollutant="TRCA")
    bad = ds.rename({"source": "member"})
    with pytest.raises(ValueError, match="has dimensions"):
        check_concentration(xr.DataTree.from_dict({"g": bad}))


def test_a_mixing_ratio_says_so_and_is_the_concentration_over_density():
    ratio = read_cdump(HERE / "ichem6", ichem=6)["TRCA"]
    plain = read_cdump(HERE / "ichem0")["TRCA"]
    assert ratio.attrs["units"].startswith("mass kg-1")
    assert "divided by air density" in ratio.attrs["long_name"]
    assert plain.attrs["units"].startswith("mass m-3")
    # Air near Salt Lake City is about 1 kg m-3.
    density = float(plain.sum() / ratio.sum())
    assert 0.8 < density < 1.1
    mass = read_cdump(HERE / "ichem6", cmass=1, ichem=6)["TRCA"]
    assert mass.attrs["units"].startswith("mass m3 kg-1")


def test_a_mixed_layer_level_reads_and_says_what_its_first_level_is():
    # Each record of the first level holds a depth (663 m here), not 50.
    with pytest.raises(ValueError, match="not the file of a run with these settings"):
        read_cdump(HERE / "ichem9_levels50")
    ds = read_cdump(HERE / "ichem9_levels50", ichem=9)
    assert ds["z"].values.tolist() == [50, 1000]
    assert "percentage of the mixed layer" in ds["z"].attrs["long_name"]
    fixed = read_cdump(HERE / "ichem0_levels50")
    assert "percentage" not in fixed["z"].attrs["long_name"]
    # Half the mixed layer is deeper than 50 m, so its concentration is lower.
    assert float(ds["TRCA"].sel(z=50).sum()) < float(fixed["TRCA"].sel(z=50).sum())
    assert float(ds["TRCA"].sel(z=50).sum()) > 0


@pytest.mark.parametrize(
    ("fixture", "fields", "dim"),
    [
        ("ichem1_3loc", {"ichem": 1}, "source"),
        ("ichem10_qcycle1", {"ichem": 10, "qcycle": 1.0}, "release_time"),
    ],
)
def test_a_runs_grids_are_read_with_its_ichem(tmp_path, fixture, fields, dim):
    shutil.copy(HERE / fixture, tmp_path / cdump_name("g"))
    tree = _read_grids(tmp_path, _concentration(**fields))
    assert tree["g"]["TRCA"].dims == (dim, *CONCENTRATION_DIMS)
    check_concentration(tree)


# -- particles enough for the emission -------------------------------------------


def test_a_grid_for_each_cycle_gets_particles_for_every_cycle():
    # With MAXPAR at one cycle's particles hycs_std 5.4.2 released no second cycle.
    one = _concentration(numpar=2000)
    cycles = _concentration(numpar=2000, ichem=10, qcycle=1.0)
    assert cycles.maxpar_for(1) == 4 * one.maxpar_for(1)

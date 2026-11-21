"""HYSPLIT's ``ichem`` values in the concentration mode, run for real on the test met cache."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from runtraj import MultiPointRelease, PointRelease
from runtraj.concentration import CONCENTRATION_DIMS
from runtraj.models import run_model
from runtraj.models.hysplit import HysplitConfig

from ..conftest import integration
from .conftest import reference_met

pytest.importorskip("netCDF4")

TIME = dt.datetime(2021, 1, 15, 0, 30)
GRID = {"name": "local", "levels": [0, 100]}


def _run(tmp_path, met_dir, release=None, **fields):
    release = release or PointRelease(
        time=TIME, longitude=-112.0, latitude=40.5, altitude=10.0
    )
    fields.setdefault("pollutants", [{"name": "TRCA"}])
    fields.setdefault("grids", [GRID])
    config = HysplitConfig(n_hours=3, numpar=500, mode="concentration", **fields)
    work = tmp_path / "work"
    run = run_model("hysplit", release, config, reference_met(met_dir), work)
    return run.result["local"].to_dataset(), work


@integration
def test_each_point_of_a_release_gets_its_own_grid(tmp_path, met_dir):
    release = MultiPointRelease(
        time=TIME,
        longitudes=[-112.0, -111.8],
        latitudes=[40.5, 40.7],
        altitudes=[10.0, 10.0],
    )
    ds, _ = _run(tmp_path, met_dir, release, ichem=1)
    assert ds["TRCA"].dims == ("source", *CONCENTRATION_DIMS)
    assert ds["source"].values.tolist() == [1, 2]
    np.testing.assert_allclose(ds["source_lon"], [-112.0, -111.8], atol=1e-4)
    first = ds["TRCA"].isel(time=0).sel(z=100)
    for k in (1, 2):
        peak = first.sel(source=k)
        assert float(peak.max()) > 0
        at = peak.where(peak == peak.max(), drop=True)
        assert abs(float(at["lon"][0]) - float(ds["source_lon"].sel(source=k))) < 0.2


@integration
def test_each_hour_of_emission_gets_its_own_grid(tmp_path, met_dir):
    ds, _ = _run(tmp_path, met_dir, ichem=10, qcycle=1.0)
    assert ds["TRCA"].dims == ("release_time", *CONCENTRATION_DIMS)
    starts = [pd.Timestamp(TIME) + pd.Timedelta(hours=k) for k in range(3)]
    assert pd.DatetimeIndex(ds["release_time"].values).tolist() == starts
    per_cycle = ds["TRCA"].sum(("z", "lat", "lon"))
    for k in range(3):
        assert float(per_cycle.isel(release_time=k, time=slice(0, k)).sum()) == 0
        assert float(per_cycle.isel(release_time=k, time=k)) > 0


@integration
def test_a_pollutant_converts_into_another(tmp_path, met_dir):
    ds, work = _run(
        tmp_path,
        met_dir,
        ichem=2,
        maxdim=2,
        pollutants=[{"name": "SO2"}, {"name": "SO4", "rate": 0.0}],
        conversions=[{"source": "SO2", "product": "SO4", "rate": 0.5}],
    )
    assert (work / "CHEMRATE.TXT").read_text() == "1 2 0.5 1.0\n"
    so2 = ds["SO2"].sel(z=100).sum(("lat", "lon"))
    so4 = ds["SO4"].sel(z=100).sum(("lat", "lon"))
    # Nothing is released as SO4: all of it was SO2, and more of it each hour.
    assert float(so4.isel(time=-1)) > 0
    share = (so4 / (so2 + so4)).values
    assert (np.diff(share) > 0).all()


@integration
@pytest.mark.parametrize(
    ("fields", "units"),
    [
        ({"ichem": 6}, "mass kg-1"),
        ({"ichem": 9, "grids": [{"name": "local", "levels": [50, 2000]}]}, "mass m-3"),
        (
            {
                "ichem": 5,
                "pollutants": [{"name": "TRCA", "deposition_velocity": 0.01}],
            },
            "mass m-3",
        ),
    ],
)
def test_the_values_that_change_what_a_cell_holds_run(tmp_path, met_dir, fields, units):
    ds, _ = _run(tmp_path, met_dir, **fields)
    assert ds["TRCA"].dims == CONCENTRATION_DIMS
    assert ds["TRCA"].attrs["units"].startswith(units)
    assert ds.sizes["time"] == 3
    assert float(ds["TRCA"].isel(z=-1).max()) > 0
    if fields["ichem"] == 5:
        assert float(ds["TRCA"].sel(z=0).max()) > 0  # deposited particles

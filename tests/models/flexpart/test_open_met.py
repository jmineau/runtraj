"""Tests for FLEXPART's open_met hook, with made-up Datasets in cfgrib's layout in place of GRIB files."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from runtraj.models.flexpart import FlexpartMetConfig, FlexpartModel
from runtraj.models.flexpart.met import MARGIN_CELLS
from runtraj.spatial import Bounds

cfgrib = pytest.importorskip("cfgrib")
converters = pytest.importorskip("arlmet.converters")

#: A 0.5 degree grid over the western US, rows north to south as GRIB stores them.
LATS = np.arange(50.0, 30.0 - 0.01, -0.5)
LONS = np.arange(235.0, 260.0 + 0.01, 0.5)
PRESSURES = np.array([1000.0, 850.0, 700.0, 500.0])
#: NCEP's HPBL, which ecCodes does not name: cfgrib calls it ``unknown``.
HPBL_KEYS = {
    "GRIB_discipline": 0,
    "GRIB_parameterCategory": 3,
    "GRIB_parameterNumber": 196,
}


def _cube(variables, valid_time, level, attrs=None):
    """One Dataset as cfgrib gives it: scalar time, step and valid time, a level coordinate."""
    type_of_level, value = level
    coords = {
        "time": pd.Timestamp(valid_time),
        "step": pd.Timedelta(0),
        "valid_time": pd.Timestamp(valid_time),
        "latitude": LATS,
        "longitude": LONS,
        type_of_level: value,
    }
    return xr.Dataset(
        {
            name: (
                dims,
                values,
                {"GRIB_typeOfLevel": type_of_level, "GRIB_stepType": "instant"}
                | (attrs or {}),
            )
            for name, (dims, values) in variables.items()
        },
        coords=coords,
    )


def gfs_file(valid_time):
    """
    The Datasets of one made-up GFS file: cfgrib.open_datasets's, and HPBL's on its own.

    open_datasets leaves HPBL out, as it does in a real forecast file, where
    a second unnamed field shares its level.
    """
    rng = np.random.default_rng(0)
    ny, nx, nz = len(LATS), len(LONS), len(PRESSURES)
    up = ("isobaricInhPa", "latitude", "longitude")
    flat = ("latitude", "longitude")
    heights = np.broadcast_to(
        np.array([100.0, 1500.0, 3000.0, 5600.0])[:, None, None], (nz, ny, nx)
    )
    cubes = [
        _cube(
            {
                "gh": (up, heights.copy()),
                "t": (up, 280.0 - heights / 150.0),
                "u": (up, rng.normal(5.0, 2.0, (nz, ny, nx))),
                "v": (up, rng.normal(0.0, 2.0, (nz, ny, nx))),
                "q": (up, np.full((nz, ny, nx), 0.004)),
            },
            valid_time,
            ("isobaricInhPa", PRESSURES),
        ),
        _cube(
            {
                "sp": (flat, np.full((ny, nx), 85000.0)),
                "orog": (flat, np.full((ny, nx), 1400.0)),
            },
            valid_time,
            ("surface", 0.0),
        ),
    ]
    hpbl = _cube(
        {"unknown": (flat, np.full((ny, nx), 1800.0))},
        valid_time,
        ("surface", 0.0),
        HPBL_KEYS,
    )
    return cubes, hpbl


@pytest.fixture
def opened(monkeypatch, tmp_path):
    """Stand cfgrib's opens in for the made-up files, and record how they were called; c.grb2 has no HPBL."""
    calls = []
    times = {
        "a.grb2": "2023-06-01 00:00",
        "b.grb2": "2023-06-01 03:00",
        "c.grb2": "2023-06-01 06:00",
    }

    def open_datasets(path, **kwargs):
        calls.append(("open_datasets", Path(path).name, kwargs))
        return gfs_file(times[Path(path).name])[0]

    def open_dataset(path, engine=None, backend_kwargs=None, **kwargs):
        assert engine == "cfgrib"
        calls.append(("open_dataset", Path(path).name, backend_kwargs))
        if Path(path).name == "c.grb2":
            return xr.Dataset()  # what cfgrib gives when no message matches
        return gfs_file(times[Path(path).name])[1]

    monkeypatch.setattr(cfgrib, "open_datasets", open_datasets)
    monkeypatch.setattr(xr, "open_dataset", open_dataset)
    files = [tmp_path / name for name in times]
    return files, calls


def gfs(tmp_path, source="gfs"):
    return FlexpartMetConfig(directory=tmp_path, source=source)


def test_open_met_converts_gfs_with_the_pbl_height(opened, tmp_path):
    files, calls = opened
    ds = FlexpartModel().open_met(gfs(tmp_path), files)
    assert ds.attrs["source"] == "GFS"
    assert ds.sizes["time"] == 3
    assert {"HGTS", "TEMP", "UWND", "VWND", "SPHU", "PRSS", "SHGT", "PBLH"} <= set(
        ds.data_vars
    )
    np.testing.assert_allclose(ds["PBLH"].isel(time=[0, 1]).values, 1800.0)
    # A file without HPBL has no PBLH at its time.
    assert ds["PBLH"].isel(time=2).isnull().all()
    # The whole grid without bounds.
    assert ds.sizes["lat"] == len(LATS)
    assert ds.sizes["lon"] == len(LONS)


def test_open_met_opens_hpbl_on_its_own_and_keeps_cfgrib_s_index(opened, tmp_path):
    files, calls = opened
    FlexpartModel().open_met(gfs(tmp_path), files)
    names = [name for _, name, _ in calls]
    assert names == ["a.grb2", "a.grb2", "b.grb2", "b.grb2", "c.grb2", "c.grb2"]
    for how, _, kwargs in calls:
        if how == "open_dataset":
            assert kwargs["filter_by_keys"] == converters.GFS_HPBL
            assert kwargs["read_keys"] == list(converters.GFS_HPBL)
            assert "indexpath" not in kwargs
        else:
            assert "indexpath" not in kwargs.get("backend_kwargs", {})


def test_open_met_crops_to_the_bounds_and_a_margin(opened, tmp_path):
    files, _ = opened
    bounds = Bounds(xmin=-113.0, xmax=-111.0, ymin=40.0, ymax=41.0)
    ds = FlexpartModel().open_met(gfs(tmp_path), files, bounds)
    margin = MARGIN_CELLS * 0.5
    lon = ds["lon"].values % 360.0
    lat = ds["lat"].values
    assert lon.min() == pytest.approx(247.0 - margin)
    assert lon.max() == pytest.approx(249.0 + margin)
    assert lat.min() == pytest.approx(40.0 - margin)
    assert lat.max() == pytest.approx(41.0 + margin)


def test_open_met_keeps_every_longitude_of_a_box_round_the_earth(opened, tmp_path):
    files, _ = opened
    bounds = Bounds(xmin=-180.0, xmax=179.0, ymin=40.0, ymax=41.0)
    ds = FlexpartModel().open_met(gfs(tmp_path), files, bounds)
    assert ds.sizes["lon"] == len(LONS)


def test_open_met_converts_era5_with_flex_extract_s_converter(monkeypatch, tmp_path):
    """Every Dataset of every file goes to from_flex_extract once, in order; bounds crop nothing."""
    opened = {
        "EA23060100": [xr.Dataset({"t": 1.0}), xr.Dataset({"sp": 2.0})],
        "EA23060101": [xr.Dataset({"t": 3.0})],
    }
    received = []

    def from_flex_extract(datasets):
        received.extend(datasets)
        return xr.Dataset({"TEMP": 0.0})

    monkeypatch.setattr(
        cfgrib, "open_datasets", lambda path, **kwargs: opened[Path(path).name]
    )
    monkeypatch.setattr(converters, "from_flex_extract", from_flex_extract)
    files = [tmp_path / name for name in opened]
    bounds = Bounds(xmin=-112.0, xmax=-111.0, ymin=40.0, ymax=41.0)
    ds = FlexpartModel().open_met(gfs(tmp_path, "era5"), files, bounds)
    assert list(ds.data_vars) == ["TEMP"]
    assert received == [*opened["EA23060100"], *opened["EA23060101"]]


def test_open_met_names_an_empty_era5_file_list(tmp_path):
    with pytest.raises(ValueError, match="No GRIB files"):
        FlexpartModel().open_met(gfs(tmp_path, "era5"), [])

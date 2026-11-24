"""Tests for FLEXPART's met opened for sampling (FlexpartMetConfig.open), with made-up Datasets in cfgrib's layout in place of GRIB files."""

from pathlib import Path

import pytest
import xarray as xr

from runtraj.models.flexpart import FlexpartMetConfig
from runtraj.spatial import Bounds

cfgrib = pytest.importorskip("cfgrib")
converters = pytest.importorskip("arlmet.converters")


def era5(tmp_path):
    return FlexpartMetConfig(directory=tmp_path)


def test_era5_opens_through_flex_extract_s_converter(monkeypatch, tmp_path):
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
    ds = era5(tmp_path).open(files, bounds)
    assert list(ds.data_vars) == ["TEMP"]
    assert received == [*opened["EA23060100"], *opened["EA23060101"]]


def test_an_empty_era5_file_list_is_named(tmp_path):
    with pytest.raises(ValueError, match="No GRIB files"):
        era5(tmp_path).open([])

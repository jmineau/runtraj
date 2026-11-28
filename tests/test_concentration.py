"""The concentration result's file: written compressed, read back whole."""

import netCDF4
import numpy as np
import pandas as pd
import xarray as xr

from runtraj.concentration import (
    describe_grid,
    read_concentration,
    write_concentration,
)


def _tree() -> xr.DataTree:
    values = np.zeros((2, 1, 50, 50), dtype="float32")
    values[0, 0, 10, 10] = 3.0
    grid = xr.Dataset(
        {"SO2": (("time", "z", "lat", "lon"), values)},
        coords={
            "time": pd.date_range("2024-01-01", periods=2, freq="h"),
            "z": [100],
            "lat": np.arange(50) * 0.01 + 40,
            "lon": np.arange(50) * 0.01 - 112,
        },
    )
    return xr.DataTree.from_dict({"local": grid})


def test_a_concentration_file_is_compressed_and_reads_back(tmp_path, point_release):
    path = write_concentration(tmp_path / "r.nc", _tree(), point_release, {}, [])
    with netCDF4.Dataset(path) as nc:
        assert nc.groups["local"].variables["SO2"].filters()["zlib"]
    back = read_concentration(path)
    assert float(back["local"]["SO2"].sum()) == 3.0


def test_a_grids_longitude_and_latitude_carry_the_cf_attributes_of_an_axis():
    grid = _tree()["local"].to_dataset().assign(time_end=lambda ds: ds["time"])
    describe_grid(grid, "mass m-3", "{name} concentration")
    lon, lat = grid["lon"].attrs, grid["lat"].attrs
    assert (lon["standard_name"], lon["axis"]) == ("longitude", "X")
    assert (lat["standard_name"], lat["axis"]) == ("latitude", "Y")
    assert (lon["units"], lat["units"]) == ("degrees_east", "degrees_north")
    assert lon["long_name"] == "longitude of the cell center"

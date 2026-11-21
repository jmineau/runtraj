"""HYSPLIT's concentration file, read against HYSPLIT's own dump of it (``cdump/README.md``)."""

import struct
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from runtraj.concentration import CONCENTRATION_DIMS
from runtraj.models.hysplit import read_cdump

HERE = Path(__file__).parent / "cdump"


def _con2asc(path: Path) -> pd.DataFrame:
    """Return a ``con2asc -s -x`` dump as one row per cell, pollutant, and level."""
    text = pd.read_csv(path, sep=r"\s+")
    rows = text.melt(
        id_vars=["YEAR", "MO", "DA", "HR", "LAT", "LON"], var_name="column"
    )
    rows["pollutant"] = rows["column"].str[:4]
    rows["z"] = rows["column"].str[4:].astype(int)
    rows["hour"] = pd.to_datetime(
        dict(year=rows.YEAR, month=rows.MO, day=rows.DA, hour=rows.HR)
    )
    return rows


@pytest.mark.parametrize(
    ("name", "dump"),
    [
        ("average_cpack1", "average.con2asc.txt"),
        ("average_cpack0", "average.con2asc.txt"),
        ("snapshot_cpack1", "snapshot.con2asc.txt"),
    ],
)
def test_every_cell_matches_hysplits_own_dump(name, dump):
    ds = read_cdump(HERE / name)
    expected = _con2asc(HERE / dump)
    # con2asc prints each sample by the hour of its end.
    end_hours = pd.DatetimeIndex(ds["time_end"].values).floor("h")
    k = end_hours.get_indexer(expected["hour"])
    i = np.abs(ds["lat"].values[None, :] - expected["LAT"].values[:, None]).argmin(1)
    j = np.abs(ds["lon"].values[None, :] - expected["LON"].values[:, None]).argmin(1)
    got = np.array(
        [
            float(ds[p].isel(time=t, lat=a, lon=b).sel(z=z))
            for p, z, t, a, b in zip(
                expected["pollutant"], expected["z"], k, i, j, strict=True
            )
        ]
    )
    assert (k >= 0).all()
    # con2asc writes 4 significant digits.
    np.testing.assert_allclose(got, expected["value"], rtol=1e-3, atol=1e-30)
    # And the cells con2asc does not list are zero.
    listed = sum(int((v > 0).sum()) for v in ds.data_vars.values())
    assert listed == int((expected["value"] > 0).sum())


def test_packed_and_full_files_read_the_same():
    packed = read_cdump(HERE / "average_cpack1")
    full = read_cdump(HERE / "average_cpack0")
    assert packed.attrs["cpack"] == 1 and full.attrs["cpack"] == 0
    for name in ("TRCA", "TRCB"):
        np.testing.assert_array_equal(packed[name].values, full[name].values)


def test_a_grid_has_its_coordinates_and_samples():
    ds = read_cdump(HERE / "average_cpack1")
    assert list(ds.data_vars) == ["TRCA", "TRCB"]
    assert all(ds[v].dims == CONCENTRATION_DIMS for v in ds.data_vars)
    assert ds.sizes == {"time": 4, "z": 2, "lat": 21, "lon": 21}
    assert list(ds["z"].values) == [0, 100]
    assert ds["time"].values[0] == np.datetime64("2021-07-15T01:30")
    assert ds["time_end"].values[0] == np.datetime64("2021-07-15T02:30")
    # Centered on the release: 1 degree across in 0.05 degree cells.
    assert ds["lat"].values[10] == pytest.approx(40.766, abs=1e-4)
    assert ds["lon"].values[10] == pytest.approx(-111.848, abs=1e-4)
    assert np.diff(ds["lat"].values) == pytest.approx(0.05, abs=1e-6)
    assert ds.attrs["met_model"] == "HRRR"


def test_a_snapshot_starts_and_ends_at_once():
    ds = read_cdump(HERE / "snapshot_cpack1")
    assert (ds["time"].values == ds["time_end"].values).all()
    assert list(ds["z"].values) == [500]


def test_a_cut_off_file_is_refused(tmp_path):
    whole = (HERE / "average_cpack1").read_bytes()
    path = tmp_path / "cdump"
    path.write_bytes(whole[: len(whole) - 10])
    with pytest.raises(ValueError, match="cut off"):
        read_cdump(path)


def _record(body: bytes) -> bytes:
    return struct.pack(">i", len(body)) + body + struct.pack(">i", len(body))


def test_a_one_point_file_is_refused(tmp_path):
    path = tmp_path / "cdump"
    path.write_bytes(_record(b"HRRR" + struct.pack(">7i", 21, 7, 15, 0, 0, 1, 2)))
    with pytest.raises(ValueError, match="CPACK=2"):
        read_cdump(path)


def test_a_file_that_is_not_a_concentration_file_is_refused(tmp_path):
    path = tmp_path / "cdump"
    path.write_bytes(b"not a HYSPLIT file at all")
    with pytest.raises(ValueError):
        read_cdump(path)


def test_a_cmass_run_is_labeled_as_mass_not_concentration():
    path = HERE / "average_cpack1"
    concentration = read_cdump(path)
    mass = read_cdump(path, cmass=1)
    assert "m-3" in concentration["TRCA"].attrs["units"]
    assert concentration["TRCA"].attrs["long_name"].startswith("TRCA concentration")
    assert "m-3" not in mass["TRCA"].attrs["units"]
    assert mass["TRCA"].attrs["units"].startswith("mass (")
    assert mass["TRCB"].attrs["long_name"].startswith("TRCB mass in the cell")
    # The coordinates say the same whatever the variables hold.
    assert mass["lat"].attrs == concentration["lat"].attrs
    np.testing.assert_array_equal(mass["TRCA"].values, concentration["TRCA"].values)


def test_the_cells_of_a_grid_are_the_decimal_degrees_it_was_given():
    ds = read_cdump(HERE / "average_cpack1")
    # The file holds 4-byte floats: 40.266 is 40.26599884 there.
    np.testing.assert_array_equal(ds["lat"].values[:2], [40.266, 40.316])
    np.testing.assert_array_equal(ds["lon"].values[:2], [-112.348, -112.298])
    assert float(ds["TRCA"].sel(lat=40.416, lon=-112.248, z=100).isel(time=0)) >= 0
    snapshot = read_cdump(HERE / "snapshot_cpack1")
    np.testing.assert_array_equal(snapshot["lat"].values[:3], [39.7, 39.8, 39.9])
    np.testing.assert_array_equal(snapshot["lon"].values[:2], [-112.9, -112.8])


def _empty_grid(path: Path, nlat, nlon, dlat, dlon, lat1, lon1) -> Path:
    """Write a ``cdump`` with a header and no sample: one pollutant, one level."""
    body = [
        b"HRRR" + struct.pack(">7i", 21, 7, 15, 0, 0, 1, 1),
        struct.pack(">4i3fi", 21, 7, 15, 0, 40.0, -112.0, 10.0, 0),
        struct.pack(">2i4f", nlat, nlon, dlat, dlon, lat1, lon1),
        struct.pack(">2i", 1, 100),
        struct.pack(">i", 1) + b"TEST",
    ]
    path.write_bytes(b"".join(_record(b) for b in body))
    return path


@pytest.mark.parametrize("first", [-179.9, 359.0, -112.0])
def test_a_fine_spacing_keeps_every_decimal_it_has(tmp_path, first):
    # 0.001 degree is a few steps of a 4-byte float near 180 or 360 degrees.
    path = _empty_grid(tmp_path / "cdump", 2, 300, 0.001, 0.001, 40.0, first)
    ds = read_cdump(path)
    np.testing.assert_array_equal(
        ds["lon"].values, np.round(first + 0.001 * np.arange(300), 3)
    )
    np.testing.assert_array_equal(ds["lat"].values, [40.0, 40.001])

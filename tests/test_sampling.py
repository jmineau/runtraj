"""Tests for runtraj.sampling: a gridded field read at points."""

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from runtraj.sampling import nearest_cell, sample_field, vertical_dim


def _flux(values=None, lons=(-112.0, -111.0, -110.0), lats=(40.0, 41.0)):
    values = (
        np.arange(len(lats) * len(lons), dtype=float).reshape(len(lats), len(lons))
        if values is None
        else values
    )
    return xr.DataArray(
        values, dims=["lat", "lon"], coords={"lat": list(lats), "lon": list(lons)}
    )


def test_sample_field_with_zero_fill_nearest_cell_and_outside_is_zero():
    flux = _flux()  # rows: lat 40 -> [0,1,2], lat 41 -> [3,4,5]

    sampled = sample_field(
        flux,
        x=[-112.0, -111.4, -110.6, -109.4, -112.6, -111.0],
        y=[40.0, 40.4, 40.6, 41.0, 40.0, 41.6],
        fill_value=0.0,
    )

    # nearest centres: (-112,40)=0, (-111,40)=1, (-111,41)=4, outside east, outside west, outside north
    assert sampled.tolist() == [0.0, 1.0, 4.0, 0.0, 0.0, 0.0]


def test_sample_field_with_zero_fill_edges_extend_half_a_cell():
    flux = _flux()
    inside = sample_field(flux, x=[-112.49, -109.51], y=[39.51, 41.49], fill_value=0.0)
    outside = sample_field(flux, x=[-112.51, -109.49], y=[39.49, 41.51], fill_value=0.0)
    assert inside.tolist() == [0.0, 5.0]
    assert outside.tolist() == [0.0, 0.0]


def test_sample_field_with_zero_fill_descending_latitude_axis():
    flux = _flux(values=np.array([[3.0, 4.0, 5.0], [0.0, 1.0, 2.0]]), lats=(41.0, 40.0))
    assert sample_field(
        flux, x=[-111.0, -111.0], y=[40.0, 41.0], fill_value=0.0
    ).tolist() == [1.0, 4.0]


def test_sample_field_with_zero_fill_nan_cells_count_as_zero():
    values = np.arange(6, dtype=float).reshape(2, 3)
    values[0, 1] = np.nan
    assert sample_field(
        _flux(values), x=[-111.0], y=[40.0], fill_value=0.0
    ).tolist() == [0.0]


def test_sample_field_leaves_missing_cells_missing_by_default():
    values = np.array([[np.nan, 1.0, 2.0], [3.0, 4.0, 5.0]])
    sampled = sample_field(_flux(values), x=[-112.0, -111.0, -100.0], y=[40.0] * 3)
    assert np.isnan(sampled[0]) and sampled[1] == 1.0 and np.isnan(sampled[2])


def test_sample_field_with_zero_fill_time_varying_uses_nearest_time():
    times = pd.to_datetime(["2023-01-01 00:00", "2023-01-01 06:00"])
    flux = xr.DataArray(
        np.stack([np.full((2, 3), 1.0), np.full((2, 3), 10.0)]),
        dims=["time", "lat", "lon"],
        coords={"time": times, "lat": [40.0, 41.0], "lon": [-112.0, -111.0, -110.0]},
    )

    sampled = sample_field(
        flux,
        x=[-111.0, -111.0, -111.0],
        y=[40.0, 40.0, 40.0],
        times=["2023-01-01 02:00", "2023-01-01 05:00", "2023-01-02 00:00"],
        fill_value=0.0,
    )

    assert sampled.tolist() == [1.0, 10.0, 10.0]
    with pytest.raises(ValueError, match="pass times"):
        sample_field(flux, x=[-111.0], y=[40.0], fill_value=0.0)


def test_sample_field_with_zero_fill_rejects_mismatched_lengths():
    with pytest.raises(ValueError, match="same length"):
        sample_field(_flux(), x=[0.0, 1.0], y=[0.0], fill_value=0.0)


def _bounded_flux(starts, ends, values=None):
    """Return a flux uniform in space whose ``time`` is a CF-bounds IntervalIndex."""
    values = np.arange(1.0, len(starts) + 1) if values is None else np.asarray(values)
    bounds = pd.IntervalIndex.from_arrays(starts, ends, closed="left")
    return xr.DataArray(
        values[:, None, None] * np.ones((len(starts), 2, 3)),
        dims=("time", "lat", "lon"),
        coords={"time": bounds, "lat": [40.0, 41.0], "lon": [-112.0, -111.0, -110.0]},
    )


def test_sample_field_step_starts_reads_cf_time_bounds_when_present():
    starts = pd.to_datetime(
        ["2023-01-01 00:00", "2023-01-01 01:00", "2023-01-01 02:00"]
    )
    ends = starts + pd.Timedelta("1h")
    flux = _bounded_flux(starts, ends)

    times = [
        "2022-12-31 23:00",  # before the first bound
        "2023-01-01 00:30",  # inside step 0
        "2023-01-01 01:30",  # inside step 1
        "2023-01-01 02:59",  # inside step 2
        "2023-01-01 03:00",  # at the last bound's end: outside, unlike the no-bounds rule
    ]
    sampled = sample_field(
        flux,
        x=[-111.0] * 5,
        y=[40.0] * 5,
        times=times,
        fill_value=0.0,
        step_starts=True,
    )
    assert sampled.tolist() == [0.0, 1.0, 2.0, 3.0, 0.0]


def test_sample_field_step_starts_reads_bounds_even_when_the_label_disagrees():
    # the bounds, not the (here middle-of-step) label, decide which step a time falls in
    starts = pd.to_datetime(["2023-01-01 00:00", "2023-01-01 01:00"])
    ends = starts + pd.Timedelta("1h")
    flux = _bounded_flux(starts, ends)
    assert flux.indexes["time"][0].left == starts[0]  # the interval carries both edges

    sampled = sample_field(
        flux,
        x=[-111.0, -111.0],
        y=[40.0, 40.0],
        times=["2023-01-01 00:45", "2023-01-01 01:45"],
        fill_value=0.0,
        step_starts=True,
    )
    assert sampled.tolist() == [1.0, 2.0]


def test_sample_field_step_starts_a_gap_between_bounds_gets_zero():
    starts = pd.to_datetime(["2023-01-01 00:00", "2023-01-01 02:00"])  # a missing hour
    ends = starts + pd.Timedelta("1h")
    flux = _bounded_flux(starts, ends)

    sampled = sample_field(
        flux,
        x=[-111.0],
        y=[40.0],
        times=["2023-01-01 01:30"],
        fill_value=0.0,
        step_starts=True,
    )
    assert sampled.tolist() == [0.0]


def test_sample_field_step_starts_refuses_overlapping_bounds():
    starts = pd.to_datetime(["2023-01-01 00:00", "2023-01-01 00:30"])
    ends = starts + pd.Timedelta("1h")  # the two steps overlap by 30 minutes
    flux = _bounded_flux(starts, ends)

    with pytest.raises(ValueError, match="overlap"):
        sample_field(
            flux,
            x=[-111.0],
            y=[40.0],
            times=["2023-01-01 00:45"],
            fill_value=0.0,
            step_starts=True,
        )


# -- a vertical dimension and longitudes ----------------------------------------


def _field_3d():
    """Levels 1000, 800, 600 hPa; value = 100 * level index + cell index."""
    base = np.arange(6, dtype=float).reshape(2, 3)
    return xr.DataArray(
        np.stack([base, base + 100.0, base + 200.0]),
        dims=["pres", "lat", "lon"],
        coords={
            "pres": [1000.0, 800.0, 600.0],
            "lat": [40.0, 41.0],
            "lon": [-112.0, -111.0, -110.0],
        },
    )


def test_vertical_dim_is_the_one_dimension_besides_horizontal_and_time():
    assert vertical_dim(_flux()) is None
    assert vertical_dim(_field_3d()) == "pres"
    timed = _field_3d().expand_dims(time=pd.to_datetime(["2023-01-01"]))
    assert vertical_dim(timed) == "pres"
    with pytest.raises(ValueError, match="at most one vertical"):
        vertical_dim(_field_3d().expand_dims(ens=[0, 1]))


def test_sample_field_is_nearest_cell_and_nan_outside():
    sampled = sample_field(
        _flux(),
        x=[-112.0, -111.4, -110.6, -109.4, -111.0],
        y=[40.0, 40.4, 40.6, 41.0, 41.6],
    )
    assert sampled[:3].tolist() == [0.0, 1.0, 4.0]
    assert np.isnan(sampled[3:]).all()


def test_sample_field_vertical_is_nearest_level_held_at_the_ends():
    sampled = sample_field(
        _field_3d(),
        x=[-112.0] * 4,
        y=[40.0] * 4,
        z=[1000.0, 650.0, 500.0, 1100.0],  # exact, nearest, below bottom, above top
    )
    assert sampled.tolist() == [0.0, 200.0, 200.0, 0.0]


def test_sample_field_wraps_longitudes_to_the_field_convention():
    east = _flux(lons=(248.0, 249.0, 250.0))  # 0..360
    assert sample_field(east, x=[-111.0], y=[40.0]).tolist() == [1.0]
    assert sample_field(_flux(), x=[249.0], y=[40.0]).tolist() == [1.0]


def test_sample_field_requires_z_and_times_when_the_field_has_them():
    with pytest.raises(ValueError, match="pass z"):
        sample_field(_field_3d(), x=[-112.0], y=[40.0])
    timed = _flux().expand_dims(time=pd.to_datetime(["2023-01-01"]))
    with pytest.raises(ValueError, match="pass times"):
        sample_field(timed, x=[-112.0], y=[40.0])
    with pytest.raises(ValueError, match="same length"):
        sample_field(_field_3d(), x=[-112.0], y=[40.0], z=[1.0, 2.0])


def test_nearest_cell_reaches_halfway_and_flags_outside():
    centers = np.array([0.0, 1.0, 2.0])
    assert nearest_cell(centers, np.array([-0.5, 0.49, 0.51, 2.5, 2.6])).tolist() == [
        0,
        0,
        1,
        2,
        -1,
    ]
    assert nearest_cell(centers[::-1], np.array([0.0, 2.0])).tolist() == [2, 0]

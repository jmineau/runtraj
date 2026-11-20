"""Tests for runtraj.sampling: a gridded field and met files read at points."""

from pathlib import Path
from unittest.mock import patch

import arlmet
import numpy as np
import pandas as pd
import pytest
import xarray as xr

from runtraj import sampling
from runtraj.models.hysplit import HysplitConfig
from runtraj.sampling import nearest_cell, sample_field, sample_met, vertical_dim


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


# ---------------------------------------------------------------------------
# sample_met: met columns at particle positions
# ---------------------------------------------------------------------------

#: Met times of the synthetic file, an hour apart.
_T0 = pd.Timestamp("2021-01-15 00:00")
_T1 = pd.Timestamp("2021-01-15 01:00")
#: Heights of its levels above ground; level 0 is the surface.
_LEVELS = [0.0, 50.0, 500.0, 2000.0]


def _met_file(path: Path, *, winds: bool = False, axis=None) -> Path:
    """
    Write an ARL file with two times of uniform fields, and return its path.

    A 1 degree lat-lon grid from 20 E, 10 S, with terrain-following levels
    at the heights in ``_LEVELS``. Every field is uniform across the grid.
    From the first time to the second, PBLH goes from 500 to 800 m and each
    level's PRES rises by 10 hPa; the rest stay the same. SPHU stays above
    1/254, the smallest value ARL packing keeps in a uniform field. With
    *winds*, it also has what :func:`calc_pbl_height` reads: winds that
    grow with height, an upward heat flux, and a friction velocity. *axis*
    replaces the vertical axis.
    """
    from arlmet import File, Projection
    from arlmet import Grid as ArlGrid
    from arlmet.vertical import TerrainAxis

    grid = ArlGrid(
        projection=Projection(
            pole_lat=90.0,
            pole_lon=0.0,
            tangent_lat=1.0,
            tangent_lon=1.0,
            grid_size=0.0,
            orientation=0.0,
            cone_angle=0.0,
            sync_x=1.0,
            sync_y=1.0,
            sync_lat=-10.0,
            sync_lon=20.0,
        ),
        nx=20,
        ny=20,
    )
    ones = np.ones((20, 20), np.float32)
    with File(
        path,
        mode="w",
        source="TEST",
        grid=grid,
        vertical_axis=TerrainAxis(_LEVELS) if axis is None else axis,
    ) as f:
        for time, pblh, dp in ((_T0, 500.0, 0.0), (_T1, 800.0, 10.0)):
            rs = f.create_recordset(time)
            rs.create_datarecord("SHGT", level=0, forecast=0, data=ones * 1500)
            rs.create_datarecord("PBLH", level=0, forecast=0, data=ones * pblh)
            if winds:
                rs.create_datarecord("SHTF", level=0, forecast=0, data=ones * 150)
                rs.create_datarecord("USTR", level=0, forecast=0, data=ones * 0.4)
            for level, (pres, temp, sphu) in enumerate(
                [(850.0, 280.0, 0.010), (800.0, 275.0, 0.008), (650.0, 265.0, 0.006)],
                start=1,
            ):
                rs.create_datarecord(
                    "PRES", level=level, forecast=0, data=ones * (pres + dp)
                )
                rs.create_datarecord("TEMP", level=level, forecast=0, data=ones * temp)
                rs.create_datarecord("SPHU", level=level, forecast=0, data=ones * sphu)
                if winds:
                    for name in ("UWND", "VWND"):
                        rs.create_datarecord(
                            name, level=level, forecast=0, data=ones * 3.0 * level
                        )
    return path


def _particles_at(times, zagl):
    """Return particles at 25.5 E, 5 S, at the given times and heights."""
    return pd.DataFrame(
        {
            "particle": np.arange(1, len(times) + 1),
            "lon": 25.5,
            "lat": -5.0,
            "zagl": zagl,
            "time": pd.to_datetime(times),
        }
    )


def test_sample_met_interpolates_linearly_in_time_and_height(tmp_path):
    """The file's levels are 46.25, 462.5 and 1850 m above its 1500 m ground (Z* (1 - 1500 / 20000))."""
    met = _met_file(tmp_path / "met.arl")
    particles = _particles_at(
        [_T0, _T0 + pd.Timedelta(minutes=20), _T1], [100.0, 200.0, 300.0]
    )

    sampled = sample_met(particles, [met])

    np.testing.assert_allclose(sampled["mlht"], [500.0, 600.0, 800.0], rtol=1e-6)
    np.testing.assert_allclose(sampled["zsfc"], 1500.0, rtol=1e-6)
    # Between the first two levels every field is linear in height; a third of
    # the way to the next hour for the second row.
    up = (np.array([100.0, 200.0, 300.0]) - 46.25) / (462.5 - 46.25)
    pres = 850.0 - 50.0 * up + np.array([0.0, 10.0 / 3.0, 10.0])
    np.testing.assert_allclose(sampled["pres"], pres, rtol=1e-6)
    temp = 280.0 - 5.0 * up
    sphu = 0.010 - 0.002 * up
    dens = 100.0 * pres / (287.04 * temp * (1.0 + 0.61 * sphu))
    np.testing.assert_allclose(sampled["dens"], dens, rtol=1e-6)
    pd.testing.assert_frame_equal(sampled[particles.columns], particles)


def test_sample_met_samples_only_the_columns_asked_for(tmp_path):
    met = _met_file(tmp_path / "met.arl")
    particles = _particles_at([_T0], [100.0])

    sampled = sample_met(particles, [met], columns=["zsfc"])

    assert list(sampled.columns) == [*particles.columns, "zsfc"]


def test_sample_met_is_nan_below_the_ground_and_off_the_grid(tmp_path):
    """A column starts at the ground, so a height of zero or more has a value."""
    met = _met_file(tmp_path / "met.arl")
    particles = _particles_at([_T0, _T0, _T0], [10.0, -5.0, 100.0])
    particles.loc[2, "lon"] = 60.0  # east of the grid

    sampled = sample_met(particles, [met], columns=["pres", "zsfc"])

    assert np.isfinite(sampled["pres"].iloc[0])
    assert np.isnan(sampled["pres"].iloc[[1, 2]]).all()
    assert sampled["zsfc"].iloc[0] == pytest.approx(1500.0)
    assert np.isnan(sampled["zsfc"].iloc[2])


def test_sample_mets_default_top_is_a_runs_default_top():
    """runtraj.sampling does not import HYSPLIT's package, so a test keeps the two equal."""
    assert HysplitConfig.model_fields["z_top"].default == sampling._DEFAULT_Z_TOP


@pytest.mark.parametrize("z_top", [sampling._DEFAULT_Z_TOP, 10000.0, None])
def test_a_terrain_following_file_is_read_with_the_runs_top(tmp_path, z_top):
    met = _met_file(tmp_path / "met.arl")
    particles = _particles_at([_T0], [100.0])

    with patch("arlmet.sample_points", wraps=arlmet.sample_points) as sample_points:
        sampled = sample_met(particles, [met], columns=["pres"], z_top=z_top)

    assert sample_points.call_args.kwargs["model_top"] == z_top
    assert np.isfinite(sampled["pres"]).all()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: p.assign(pres=1.0), "already have"),
        (lambda p: p.drop(columns="time"), "'time' column"),
        (lambda p: p.assign(time=_T1 + pd.Timedelta(minutes=1)), "outside the met"),
        (lambda p: p.assign(time=_T0 - pd.Timedelta(minutes=1)), "outside the met"),
    ],
    ids=["column-present", "no-time", "after-the-met", "before-the-met"],
)
def test_sample_met_rejects(tmp_path, change, message):
    met = _met_file(tmp_path / "met.arl")
    particles = change(_particles_at([_T0], [100.0]))

    with pytest.raises(ValueError, match=message):
        sample_met(particles, [met], columns=["pres"])


def test_sample_met_rejects_a_column_it_cannot_sample(tmp_path):
    met = _met_file(tmp_path / "met.arl")

    with pytest.raises(ValueError, match="cannot sample"):
        sample_met(_particles_at([_T0], [100.0]), [met], columns=["foot"])


def _richardson_column(heat: float, theta_top: float | None = None):
    """
    One column on 10 m levels: neutral air to 1000 m, stable above, and 5 m/s of shear above the lowest level.

    Without the temperature excess, the bulk Richardson number from the
    lowest level (10 m) is g (θ − θ₁)(z − 10) / (θ₁ · 25), which passes 0.25
    at about 1019 m.
    """
    from runtraj.sampling import _richardson_mixed_layer

    zz = np.arange(10.0, 3001.0, 10.0)[None, :]
    theta = 300.0 + 0.01 * np.clip(zz - 1000.0, 0.0, None)
    if theta_top is not None:
        theta = np.full_like(zz, theta_top)
    uu = np.where(zz > 10.0, 5.0, 0.0)
    return _richardson_mixed_layer(
        zz,
        theta,
        uu,
        np.zeros_like(zz),
        np.ones_like(zz),
        np.array([0.3]),
        np.array([heat]),
        ks=6,
    )


def test_richardson_mixed_layer_ends_where_the_bulk_richardson_number_passes_a_quarter():
    stable = _richardson_column(heat=-20.0)

    g = 9.80616
    expected = (
        1010 + np.sqrt(1010**2 + 4 * (0.25 * 300 * 25 / (g * 0.01) - 10000))
    ) / 2
    assert stable[0] == pytest.approx(expected, rel=0.005)


def test_richardson_mixed_layer_is_deeper_in_unstable_air():
    # Upward heat flux gives the rising air a temperature excess.
    assert _richardson_column(heat=200.0)[0] > _richardson_column(heat=-20.0)[0] + 20


def test_richardson_mixed_layer_without_a_crossing_is_the_second_highest_level():
    assert _richardson_column(heat=-20.0, theta_top=300.0)[0] == pytest.approx(2990.0)


def _flexpart_column(heat: float, lapse_above: float = 0.0, below_ground: bool = False):
    """
    One column on 50 m levels: dry-adiabatic to 1000 m, isothermal above, no wind, and u* 0.3 m/s.

    The wind shear is then FLEXPART's 100 u*² = 9 m² s⁻², the 2 m dew
    point of 250 K adds 0.09 K to the reference virtual potential
    temperature, and without the temperature excess Ri =
    g (θ − θ₂)(z − 2) / (θ₂ · 9) passes 0.25 where
    (0.0098 (z − 1000) − 0.09)(z − 2) = 68.8, at about 1016 m. With
    *lapse_above* the air above 1000 m is dry-adiabatic too. With
    *below_ground* a level under the ground comes first.
    """
    from runtraj.sampling import _flexpart_mixed_layer

    z = np.arange(0.0, 3001.0, 1.0)
    temp = 300.0 - 0.00976 * np.minimum(z, 1000.0)
    temp -= lapse_above * np.maximum(z - 1000.0, 0.0)
    pres = 1e5 * np.exp(
        -np.concatenate(
            [[0.0], np.cumsum(9.81 / (287.05 * (temp[1:] + temp[:-1]) / 2))]
        )
    )
    levels = np.arange(50, 3001, 50)
    pres, temp = pres[levels], temp[levels]
    if below_ground:
        pres, temp = np.concatenate([[100500.0], pres]), np.concatenate([[301.0], temp])
    calm = np.zeros((1, len(pres)))

    def one(value: float) -> np.ndarray:
        return np.array([value])

    return _flexpart_mixed_layer(
        pres[None, :],
        temp[None, :],
        calm,
        calm,
        calm,
        one(1e5),
        one(300.0 - 0.00976 * 2.0),
        one(250.0),
        one(0.0),
        one(0.0),
        one(0.3),
        one(heat),
    )


def test_flexpart_mixed_layer_ends_where_the_richardson_number_passes_a_quarter():
    # Found to a twentieth of the 50 m layer it falls in.
    assert _flexpart_column(heat=-20.0)[0] == pytest.approx(1016.0, abs=3.0)


def test_flexpart_mixed_layer_is_deeper_in_unstable_air():
    assert _flexpart_column(heat=200.0)[0] > _flexpart_column(heat=-20.0)[0] + 20


def test_flexpart_mixed_layer_passes_over_a_level_below_the_ground():
    assert _flexpart_column(heat=-20.0, below_ground=True)[0] == pytest.approx(
        _flexpart_column(heat=-20.0)[0]
    )


def test_flexpart_mixed_layer_without_a_top_is_nan():
    # Neutral air all the way up: FLEXPART stops there.
    assert np.isnan(_flexpart_column(heat=-20.0, lapse_above=0.00976)[0])


def test_flexpart_mixed_layer_is_at_least_100_m():
    from runtraj.sampling import _flexpart_mixed_layer

    # Stable from the first level: the top is in the lowest layer.
    pres = np.array([[99000.0, 98000.0, 97000.0]])
    temp = np.array([[300.0, 302.0, 304.0]])
    calm = np.zeros_like(pres)
    one = np.ones(1)
    h = _flexpart_mixed_layer(
        pres,
        temp,
        calm,
        calm,
        calm,
        1e5 * one,
        295.0 * one,
        250.0 * one,
        0 * one,
        0 * one,
        0.1 * one,
        -20.0 * one,
    )
    assert h[0] == 100.0


def test_temperature_mixed_layer_is_the_highest_level_within_2_k_of_the_lowest():
    from runtraj.sampling import _temperature_mixed_layer

    zz = np.array([[10.0, 100.0, 500.0, 900.0, 1500.0]])
    theta = np.array([[301.0, 300.0, 301.5, 302.5, 305.0]])
    assert _temperature_mixed_layer(zz, theta)[0] == 500.0


@pytest.mark.parametrize(
    ("around", "expected"),
    [(False, {(5, 6)}), (True, {(5, 5), (6, 5), (5, 6), (6, 6)})],
    ids=["nearest", "around"],
)
def test_grid_points_read_are_the_nearest_or_the_four_around(
    tmp_path, around, expected
):
    """FLEXPART takes the greatest height of the four grid points around a particle."""
    from runtraj.sampling import _grid_points_read

    met = arlmet.open_dataset(_met_file(tmp_path / "met.arl"))
    t = np.array([_T0.to_datetime64()])

    reads = _grid_points_read(
        met.arl.grid,
        np.array([25.3]),
        np.array([-4.4]),
        t,
        t,
        np.zeros(1),
        around=around,
    )

    assert set(zip(reads["i"], reads["j"], strict=True)) == expected
    np.testing.assert_allclose(reads["lon"], 20.0 + reads["i"])
    np.testing.assert_allclose(reads["lat"], -10.0 + reads["j"])


def test_calc_pbl_height_refuses_an_unknown_method(tmp_path):
    met = _met_file(tmp_path / "met.arl", winds=True)

    with pytest.raises(ValueError, match="no method 'kmixd3'"):
        sampling.calc_pbl_height(_particles_at([_T0], [100.0]), [met], method="kmixd3")


def test_calc_pbl_height_met_is_the_met_pblh_linear_in_time(tmp_path):
    """The file's PBLH goes from 500 to 800 m in the hour."""
    met = _met_file(tmp_path / "met.arl")
    particles = _particles_at([_T0, _T0 + pd.Timedelta(minutes=20)], [100.0, 100.0])

    mlht = sampling.calc_pbl_height(particles, [met], method="met")

    np.testing.assert_allclose(mlht, [500.0, 600.0], rtol=1e-5)


def test_calc_pbl_height_names_what_flexpart_needs(tmp_path):
    met = _met_file(tmp_path / "met.arl", winds=True)

    with pytest.raises(KeyError, match="DP2M.*PRSS.*T02M.*U10M.*V10M"):
        sampling.calc_pbl_height(
            _particles_at([_T0], [100.0]), [met], method="flexpart"
        )


def test_calc_pbl_height_names_the_variables_the_files_lack(tmp_path):
    met = _met_file(tmp_path / "met.arl")

    with pytest.raises(KeyError, match="UWND"):
        sampling.calc_pbl_height(_particles_at([_T0], [100.0]), [met])


def test_calc_pbl_height_needs_a_time_column(tmp_path):
    met = _met_file(tmp_path / "met.arl")
    particles = _particles_at([_T0], [100.0]).drop(columns="time")

    with pytest.raises(ValueError, match="'time' column"):
        sampling.calc_pbl_height(particles, [met])


# ---------------------------------------------------------------------------
# Met as a Dataset: a converter's output, or any format arlmet reads
# ---------------------------------------------------------------------------


def test_sample_met_reads_a_dataset_as_the_file_it_was_read_from(tmp_path):
    met = _met_file(tmp_path / "met.arl")
    particles = _particles_at(
        [_T0, _T0 + pd.Timedelta(minutes=20), _T1], [100.0, 200.0, 300.0]
    )

    from_dataset = sample_met(particles, arlmet.open_dataset(met))

    pd.testing.assert_frame_equal(from_dataset, sample_met(particles, [met]))


@pytest.mark.parametrize("method", ["hysplit", "hysplit_temperature", "met"])
def test_calc_pbl_height_reads_a_dataset_as_the_file_it_was_read_from(tmp_path, method):
    met = _met_file(tmp_path / "met.arl", winds=True)
    particles = _particles_at([_T0, _T0 + pd.Timedelta(minutes=30)], [100.0, 100.0])

    from_dataset = sampling.calc_pbl_height(
        particles, arlmet.open_dataset(met), method=method
    )

    from_files = sampling.calc_pbl_height(particles, [met], method=method)
    assert np.isfinite(from_files).all()
    np.testing.assert_array_equal(from_dataset, from_files)


def _pressure_level_file(path: Path) -> Path:
    """
    Write an ARL file on pressure levels, as GFS's are, and return its path.

    The levels are 850, 800, 700 and 600 hPa over 1500 m of terrain where
    the surface pressure is 850 hPa: the pressure is the level, and there
    is no PRES field.
    """
    from arlmet import File, Projection
    from arlmet import Grid as ArlGrid
    from arlmet.vertical import PressureAxis

    grid = ArlGrid(
        projection=Projection(
            pole_lat=90.0, pole_lon=0.0, tangent_lat=1.0, tangent_lon=1.0,
            grid_size=0.0, orientation=0.0, cone_angle=0.0, sync_x=1.0,
            sync_y=1.0, sync_lat=-10.0, sync_lon=20.0,
        ),
        nx=20,
        ny=20,
    )  # fmt: skip
    ones = np.ones((20, 20), np.float32)
    levels = [(850.0, 1500.0, 285.0), (800.0, 1990.0, 282.0), (700.0, 3050.0, 274.0),
              (600.0, 4250.0, 266.0)]  # fmt: skip
    axis = PressureAxis(levels=[0.0, *(p for p, _, _ in levels)])
    with File(path, mode="w", source="TEST", grid=grid, vertical_axis=axis) as f:
        rs = f.create_recordset(_T0)
        rs.create_datarecord("SHGT", level=0, forecast=0, data=ones * 1500)
        rs.create_datarecord("PRSS", level=0, forecast=0, data=ones * 850)
        for level, (_, height, temp) in enumerate(levels, start=1):
            rs.create_datarecord("HGTS", level=level, forecast=0, data=ones * height)
            rs.create_datarecord("TEMP", level=level, forecast=0, data=ones * temp)
    return path


def test_met_without_pres_gives_the_pressure_of_its_levels(tmp_path):
    """Pressure levels have no PRES field: the pressure is the level."""
    met = _pressure_level_file(tmp_path / "pressure.arl")
    particles = _particles_at([_T0] * 3, [100.0, 1000.0, 2000.0])

    with patch("arlmet.sample_points", wraps=arlmet.sample_points) as sample_points:
        sampled = sample_met(particles, [met], columns=["pres"])

    assert "pressure" in sample_points.call_args.args[2]
    pres = sampled["pres"].to_numpy()
    assert np.isfinite(pres).all()
    assert (np.diff(pres) < 0).all()
    assert pres.min() > 600.0 and pres.max() < 850.0
    pd.testing.assert_frame_equal(
        sample_met(particles, arlmet.open_dataset(met), columns=["pres"]), sampled
    )


def test_terrain_following_met_without_pres_has_no_pressure(tmp_path):
    met = arlmet.open_dataset(_met_file(tmp_path / "met.arl")).drop_vars("PRES")

    with pytest.raises(KeyError, match="PRES"):
        sample_met(_particles_at([_T0], [100.0]), met, columns=["pres"])


def test_wrf_met_without_pres_has_no_pressure(tmp_path):
    """WRF's hybrid levels give pressure only from PRES, as HYSPLIT's prfwrf reads it."""
    axis = arlmet.WrfAxis(levels=[1.0, 0.995, 0.98, 0.9], offset=50.0)
    path = _met_file(tmp_path / "met.arl", axis=axis)
    met = arlmet.open_dataset(path).drop_vars("PRES")

    with pytest.raises(KeyError, match="PRES"):
        sample_met(_particles_at([_T0], [100.0]), met, columns=["pres"])


@pytest.mark.parametrize("function", [sample_met, sampling.calc_pbl_height])
def test_no_met_files_is_a_clear_error(function):
    with pytest.raises(ValueError, match="No met files"):
        function(_particles_at([_T0], [100.0]), [])


def test_calc_pbl_height_names_every_missing_variable_with_pres(tmp_path):
    met = arlmet.open_dataset(_met_file(tmp_path / "met.arl", winds=True))

    with pytest.raises(KeyError, match="'PRES', 'SHTF'"):
        sampling.calc_pbl_height(
            _particles_at([_T0], [100.0]), met.drop_vars(["PRES", "SHTF"])
        )


def test_a_variable_only_some_files_hold_counts_as_missing(tmp_path):
    """SPHU in one file but not the next: dry air at every time, not a KeyError."""
    first = _met_file(tmp_path / "first.arl")
    later = arlmet.open_dataset(first).drop_vars("SPHU").load()
    later = later.assign_coords(time=later["time"] + pd.Timedelta(hours=2))
    arlmet.write_dataset(later, tmp_path / "later.arl")
    particles = _particles_at([_T0, _T1 + pd.Timedelta(hours=1)], [100.0] * 2)

    sampled = sample_met(particles, [first, tmp_path / "later.arl"], columns=["dens"])

    dry = sample_met(particles.iloc[1:], later, columns=["dens"])
    np.testing.assert_allclose(sampled["dens"].iloc[1], dry["dens"].iloc[0])
    dry_first = sample_met(
        particles.iloc[:1], arlmet.open_dataset(first).drop_vars("SPHU"), ["dens"]
    )
    np.testing.assert_allclose(sampled["dens"].iloc[0], dry_first["dens"].iloc[0])


def test_met_without_sphu_gives_the_density_of_dry_air(tmp_path):
    met = arlmet.open_dataset(_met_file(tmp_path / "met.arl"))
    particles = _particles_at([_T0], [100.0])

    sampled = sample_met(particles, met.drop_vars("SPHU"), columns=["dens"])

    moist = sample_met(particles, met, columns=["pres", "dens"])
    up = (100.0 - 46.25) / (462.5 - 46.25)
    dry = 100.0 * moist["pres"] / (287.04 * (280.0 - 5.0 * up))
    np.testing.assert_allclose(sampled["dens"], dry, rtol=1e-6)
    assert (sampled["dens"] > moist["dens"]).all()


def test_a_particle_time_between_microseconds_is_sampled(tmp_path):
    """A time in nanoseconds is compared with met times pandas holds in microseconds."""
    met = _met_file(tmp_path / "met.arl")
    particles = _particles_at([_T0 + pd.Timedelta(minutes=20, nanoseconds=1)], [100.0])

    sampled = sample_met(particles, [met], columns=["mlht"])

    assert sampled["mlht"].iloc[0] == pytest.approx(600.0, rel=1e-6)


def test_calc_pbl_height_is_nan_where_the_met_lacks_the_heat_flux(tmp_path):
    """A GFS analysis has no fluxes; a missing one must not read as stable air."""
    met = arlmet.open_dataset(_met_file(tmp_path / "met.arl", winds=True))
    shtf = met["SHTF"].where(met["time"] != met["time"][1])
    particles = _particles_at([_T0, _T0 + pd.Timedelta(minutes=30), _T1], [100.0] * 3)

    mlht = sampling.calc_pbl_height(particles, met.assign(SHTF=shtf))

    assert np.isfinite(mlht[0])
    assert mlht[1] == mlht[0]  # between the two, the time with the fluxes
    assert np.isnan(mlht[2])


# ---------------------------------------------------------------------------
# Inputs that would give a plausible wrong value are refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lons",
    [(-112.0, -110.0, -111.0), (-112.0, -111.0, -111.0)],
    ids=["unsorted", "repeated"],
)
def test_sample_field_refuses_coordinates_out_of_order(lons):
    with pytest.raises(ValueError, match="strictly increase"):
        sample_field(_flux(lons=lons), [-111.0], [41.0])


def test_sample_field_refuses_a_field_without_coordinates():
    field = xr.DataArray(np.ones((2, 3)), dims=("y", "x"))
    with pytest.raises(ValueError, match="no coordinates"):
        sample_field(field, [1.0], [1.0])


def _hourly():
    times = pd.date_range("2023-07-15", periods=3, freq="h")
    return _flux().expand_dims(time=times).copy()


@pytest.mark.parametrize("step_starts", [False, True])
def test_sample_field_refuses_a_missing_time(step_starts):
    with pytest.raises(ValueError, match="missing"):
        sample_field(
            _hourly(), [-111.0], [41.0], times=[pd.NaT], step_starts=step_starts
        )


def test_sample_field_refuses_times_given_as_numbers():
    with pytest.raises(TypeError, match="datetimes"):
        sample_field(_hourly(), [-111.0], [41.0], times=[60.0])


@pytest.mark.parametrize("function", ["sample_met", "calc_pbl_height"])
@pytest.mark.parametrize(
    ("z_top", "message"), [(-100.0, "positive"), (0.05, "below particles")]
)
def test_a_z_top_that_is_not_the_runs_top_in_meters_is_refused(
    tmp_path, function, z_top, message
):
    met = _met_file(tmp_path / "met.arl")
    with pytest.raises(ValueError, match=message):
        getattr(sampling, function)(_particles_at([_T0], [100.0]), [met], z_top=z_top)

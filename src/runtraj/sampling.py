"""
Sampling gridded fields, such as a surface flux or a mole fraction, at points.

A field is an :class:`xarray.DataArray` on a regular ``lat``/``lon`` grid
(``y``/``x`` for a projected grid), with an optional vertical dimension and
an optional ``time`` dimension. Each point takes the value of the cell it
falls in. Use it to read a field at particle positions: an oxidant along
each trajectory (:class:`~runtraj.transforms.Oxidation`), a mole fraction
where each particle ends, or a surface flux under each particle. Units are
not converted.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr
from numpy.typing import ArrayLike
from pandas.errors import InvalidIndexError

from runtraj.spatial import horizontal_dims


def nearest_cell(coords: np.ndarray, values: np.ndarray) -> np.ndarray:
    """
    Return the index of the cell nearest each value, or ``-1`` outside the cells.

    Each cell reaches halfway to its neighbors. The end cells reach the
    same distance beyond their centers. With a single cell, every finite
    value is inside it.

    Parameters
    ----------
    coords : numpy.ndarray
        Cell centers along one axis, ascending or descending.
    values : numpy.ndarray
        Positions to look up.

    Returns
    -------
    numpy.ndarray
        Index into *coords* for each value.
    """
    coords = np.asarray(coords, dtype=float)
    if coords.ndim != 1 or coords.size == 0:
        raise ValueError("Field coordinates must be a non-empty 1-D array.")
    ascending = coords[0] <= coords[-1]
    c = coords if ascending else coords[::-1]
    if c.size == 1:
        inside = np.isfinite(values)
        return np.where(inside, 0, -1)
    mids = (c[:-1] + c[1:]) / 2.0
    lo = c[0] - (c[1] - c[0]) / 2.0
    hi = c[-1] + (c[-1] - c[-2]) / 2.0
    idx = np.searchsorted(mids, values)
    inside = (values >= lo) & (values <= hi)
    idx = np.where(inside, idx, -1)
    if not ascending:
        idx = np.where(idx >= 0, c.size - 1 - idx, -1)
    return idx


def vertical_dim(field: xr.DataArray) -> str | None:
    """
    Return the name of a field's vertical dimension.

    That is the one dimension that is neither horizontal nor ``time``.
    Returns ``None`` for a field without one, such as a column mean or a
    surface field, and raises if there is more than one candidate.
    """
    y_dim, x_dim = horizontal_dims(field)
    extra = [str(d) for d in field.dims if d not in (y_dim, x_dim, "time")]
    if len(extra) > 1:
        raise ValueError(
            f"Expected at most one vertical dimension besides {y_dim!r}/{x_dim!r} "
            f"and 'time'; got {extra}."
        )
    return extra[0] if extra else None


def _bounds_indexer(
    bounds: pd.IntervalIndex, stamps: pd.DatetimeIndex
) -> tuple[np.ndarray, np.ndarray]:
    """
    Return each stamp's step index from a flux's CF time bounds, and whether it falls in one.

    A stamp before the first bound, after the last, or in a gap between
    bounds falls in none: the bounds say where every step starts and ends,
    so there is nothing to guess at either edge.
    """
    try:
        it = bounds.get_indexer(stamps)
    except InvalidIndexError as e:
        raise ValueError(
            "The field's time bounds overlap, so a time can fall in more than "
            "one step. Two bounds cannot cover the same instant."
        ) from e
    in_step = it >= 0
    return np.where(in_step, it, 0), in_step


def sample_field(
    field: xr.DataArray,
    x: ArrayLike,
    y: ArrayLike,
    z: ArrayLike | None = None,
    times: ArrayLike | None = None,
    fill_value: float = np.nan,
    step_starts: bool = False,
) -> np.ndarray:
    """
    Return the field's value in the cell nearest each point.

    A point outside the field horizontally, or in a missing cell, gives
    ``fill_value``. Longitudes are wrapped to the field's convention (-180
    to 180 or 0 to 360).

    Parameters
    ----------
    field : xarray.DataArray
        Field with horizontal dimensions (``lat`` and ``lon``, or ``y`` and
        ``x``), and optionally a vertical dimension and ``time``.
    x, y : array-like
        Point coordinates: longitude and latitude for a ``lat``/``lon``
        field.
    z : array-like, optional
        Vertical coordinate of each point, in the units of the field's
        vertical dimension. Required when the field has one. Matched to the
        nearest level, so a point above the top level takes the top level.
    times : array-like, optional
        Time of each point. Required when the field has a ``time``
        dimension. By default matched to the nearest time, so times
        outside the field's span take the first or last step. See
        ``step_starts``.
    fill_value : float, default NaN
        Value for points outside the field and for missing cells. Use 0 for
        a surface flux, where no data means no emission. ``NaN`` suits a
        mole fraction, which is unknown there.
    step_starts : bool, default False
        Whether the field's ``time`` labels are the starts of steps, as a
        flux inventory's are. A point then takes the last step that starts
        at or before its time. A point before the first label takes
        ``fill_value`` and a point after the last takes the last step, since
        the labels do not say where it ends. The labels must strictly
        increase: a repeated label is an error. When ``field["time"]`` is a
        :class:`pandas.IntervalIndex` (its CF time bounds, read as each
        step's start and end), a point is matched by which step's bounds
        hold it instead: one before the first bound, after the last, or in
        a gap between bounds takes ``fill_value``, since the bounds say
        where every step starts and ends and there is nothing to guess at
        either edge. The bounds must not overlap.

    Returns
    -------
    numpy.ndarray
        One value per point.
    """
    y_dim, x_dim = horizontal_dims(field)
    xs = np.asarray(x, dtype=float).ravel()
    ys = np.asarray(y, dtype=float).ravel()
    if xs.shape != ys.shape:
        raise ValueError("x and y must have the same length.")
    lons = field[x_dim].to_numpy()
    if x_dim == "lon":
        xs = xs % 360.0 if lons.max() > 180.0 else ((xs + 180.0) % 360.0) - 180.0
    ix = nearest_cell(lons, xs)
    iy = nearest_cell(field[y_dim].to_numpy(), ys)
    inside = (ix >= 0) & (iy >= 0)

    indexers: dict[str, xr.DataArray] = {
        x_dim: xr.DataArray(np.where(inside, ix, 0), dims="points"),
        y_dim: xr.DataArray(np.where(inside, iy, 0), dims="points"),
    }
    zdim = vertical_dim(field)
    if zdim is not None:
        if z is None:
            raise ValueError(f"field has a {zdim!r} dimension; pass z.")
        zs = np.asarray(z, dtype=float).ravel()
        if zs.shape != xs.shape:
            raise ValueError("z must have the same length as x and y.")
        indexers[zdim] = xr.DataArray(_nearest_level(field[zdim], zs), dims="points")
    if "time" in field.dims:
        if times is None:
            raise ValueError("field has a time dimension; pass times.")
        stamps = pd.DatetimeIndex(pd.to_datetime(np.asarray(times).ravel()))
        if len(stamps) != len(xs):
            raise ValueError("times must have the same length as x and y.")
        index = field.indexes["time"]
        if step_starts:
            if isinstance(index, pd.IntervalIndex):
                it, in_step = _bounds_indexer(index, stamps)
            else:
                if not index.is_monotonic_increasing:
                    raise ValueError(
                        "The field's time labels must strictly increase to be read "
                        "as step starts."
                    )
                if not index.is_unique:
                    repeated = index[index.duplicated()][0]
                    raise ValueError(
                        "The field's time labels must strictly increase to be read "
                        f"as step starts, but {repeated} is repeated. Two steps "
                        "cannot start at the same time; drop one, such as the "
                        "overlap of two concatenated files."
                    )
                it = index.searchsorted(stamps, side="right") - 1
                in_step = it >= 0
                it = np.clip(it, 0, None)
            inside = inside & in_step
        else:
            it = index.get_indexer(stamps, method="nearest")
        indexers["time"] = xr.DataArray(it, dims="points")
    sampled = field.isel(indexers).to_numpy().astype(float)
    return np.where(inside & ~np.isnan(sampled), sampled, fill_value)


def _nearest_level(levels: xr.DataArray, z: np.ndarray) -> np.ndarray:
    """Return the index of the level nearest each ``z``."""
    coords = levels.to_numpy().astype(float)
    if coords.ndim != 1 or coords.size == 0:
        raise ValueError("The vertical coordinate must be a non-empty 1-D array.")
    return np.abs(coords[None, :] - z[:, None]).argmin(axis=1)


__all__ = [
    "nearest_cell",
    "sample_field",
    "vertical_dim",
]

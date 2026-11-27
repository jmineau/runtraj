"""
Sampling gridded fields and meteorology at points, such as particle positions.

:func:`sample_field` reads a gridded field, such as a surface flux or a
mole fraction: an :class:`xarray.DataArray` on a regular ``lat``/``lon``
grid (``y``/``x`` for a projected grid), with an optional vertical
dimension and an optional ``time`` dimension. Each point takes the value of
the cell it falls in. Use it to read a field at particle positions: an
oxidant along each trajectory (:class:`~runtraj.transforms.Oxidation`), a
mole fraction where each particle ends, or a surface flux under each
particle. Units are not converted.

:func:`sample_met` reads met at particle positions as HYSPLIT does, for
the columns HYSPLIT writes from the met (``mlht``, ``dens``, ``pres``,
``zsfc``), which a transport model that writes only positions leaves out.
:func:`calc_pbl_height` finds the mixed-layer height from the met, as the
transport model of the run did (HYSPLIT's ``kmixd``, or FLEXPART's
method). Both read ARL files, or met in another
format converted by arlmet (:meth:`runtraj.models.MetConfig.open` opens a
run's met files the way its transport model reads them).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
        Cell centers along one axis, strictly ascending or strictly descending.
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
    steps = np.diff(coords)
    if not ((steps > 0).all() or (steps < 0).all()):
        raise ValueError(
            "Field coordinates must strictly increase or strictly decrease; "
            f"got {coords[:6].tolist()}{' ...' if coords.size > 6 else ''}. "
            "Sort the field along that dimension and drop repeated values."
        )
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
    to 180 or 0 to 360), and a cell at one end of the field reaches across
    the seam: on a global field from 0 to 359.75, a point at 359.9 is in
    the cell at 0.

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
        Time of each point, as datetimes or date strings, with none
        missing. A time with a time zone is converted to UTC, which the
        field's times are taken to be in. Required when the field has a ``time``
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
    missing = [d for d in (y_dim, x_dim) if d not in field.coords]
    if missing:
        raise ValueError(
            f"The field has no coordinates for its {' and '.join(map(repr, missing))} "
            "dimensions, so a point cannot be placed in it. Assign them first."
        )
    xs = np.asarray(x, dtype=float).ravel()
    ys = np.asarray(y, dtype=float).ravel()
    if xs.shape != ys.shape:
        raise ValueError("x and y must have the same length.")
    lons = field[x_dim].to_numpy()
    if x_dim == "lon":
        xs = xs % 360.0 if lons.max() > 180.0 else ((xs + 180.0) % 360.0) - 180.0
    ix = nearest_cell(lons, xs)
    if x_dim == "lon":
        # A point past one end of the field may be in the cell at the other end, across the seam.
        for shift in (-360.0, 360.0):
            ix = np.where(ix >= 0, ix, nearest_cell(lons, xs + shift))
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
        raw = np.asarray(times).ravel()
        if raw.dtype.kind in "biufc":
            raise TypeError(
                f"times must be datetimes or date strings; got numbers ({raw.dtype})."
            )
        stamps = _utc(pd.DatetimeIndex(pd.to_datetime(raw)))
        if len(stamps) != len(xs):
            raise ValueError("times must have the same length as x and y.")
        missing = np.asarray(stamps.isna())
        if missing.any():
            raise ValueError(
                f"times holds {int(missing.sum())} missing value(s), the first "
                f"at position {int(np.flatnonzero(missing)[0])}."
            )
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


def _utc(times: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Return *times* in UTC without a time zone, as met and fields label theirs."""
    if times.tz is not None:
        return times.tz_convert("UTC").tz_localize(None)
    return times


def _check_z_top(particles: pd.DataFrame, z_top: float | None) -> None:
    """Refuse a *z_top* that is not positive, or below a particle, as one given in km would be."""
    if z_top is None:
        return
    if not z_top > 0:
        raise ValueError(f"z_top must be a positive height in meters; got {z_top:g}.")
    if "zagl" in particles.columns and len(particles):
        highest = float(np.nanmax(particles["zagl"].to_numpy(dtype=float)))
        if highest > z_top:
            raise ValueError(
                f"z_top is {z_top:g} m, below particles at up to {highest:g} m "
                "above ground. z_top is the top of the run's domain in meters."
            )


#: The line HYSPLIT prints in ``MESSAGE`` for its internal levels, in the
#: same form from 5.0.2 to 5.4.2: ``nlvl, aa, bb, cc``.
_INTERNAL_GRID_LINE = re.compile(
    r"Internal grid parameters \(nlvl,aa,bb,cc\):\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)"
)


def _read_internal_grid(
    message: str | os.PathLike[str] | None,
) -> tuple[int, float, float, float] | None:
    """Return ``(nlvl, aa, bb, cc)`` from a run's ``MESSAGE`` file (level ``k`` is ``aa k**2 + bb k + cc`` m), or None for no file."""
    if message is None:
        return None
    found = _INTERNAL_GRID_LINE.search(Path(message).read_text(errors="replace"))
    if found is None:
        raise ValueError(
            f"{message} has no 'Internal grid parameters (nlvl,aa,bb,cc)' line: "
            "message is the MESSAGE file HYSPLIT wrote in the run's working directory."
        )
    nlvl, aa, bb, cc = found.groups()
    return int(nlvl), float(aa), float(bb), float(cc)


def _nearest_level(levels: xr.DataArray, z: np.ndarray) -> np.ndarray:
    """Return the index of the level nearest each ``z``."""
    coords = levels.to_numpy().astype(float)
    if coords.ndim != 1 or coords.size == 0:
        raise ValueError("The vertical coordinate must be a non-empty 1-D array.")
    # Memory is proportional to the points, not points x levels. The nearest
    # level is one of the two neighbors of z in the sorted levels; a tie goes
    # to the lower index, and non-finite z to level 0.
    order = np.argsort(coords, kind="stable")
    c = coords[order]
    above = np.searchsorted(c, z)
    hi = np.minimum(above, c.size - 1)
    lo = np.searchsorted(c, c)[np.maximum(above - 1, 0)]  # first of repeated levels
    d_lo, d_hi = np.abs(c[lo] - z), np.abs(c[hi] - z)
    nearest = np.where(
        d_lo < d_hi,
        order[lo],
        np.where(d_hi < d_lo, order[hi], np.minimum(order[lo], order[hi])),
    )
    return np.where(np.isfinite(z), nearest, 0)


#: The ARL variables each particle column is computed from, by :func:`sample_met`.
#: ``PRES`` stands for the pressure: the met's ``PRES`` field where it has
#: one (:func:`_pressure_variable`).
_MET_VARIABLES: dict[str, tuple[str, ...]] = {
    "mlht": ("PBLH",),
    "dens": ("PRES", "TEMP", "SPHU"),
    "pres": ("PRES",),
    "zsfc": ("SHGT",),
}

#: Gas constant of dry air, J kg⁻¹ K⁻¹, the value HYSPLIT uses.
_R_DRY = 287.04

#: The top of the domain of a HYSPLIT run that does not set one, in meters
#: (``HysplitConfig.z_top``, which a test keeps this equal to: this module does
#: not import HYSPLIT's package).
_DEFAULT_Z_TOP = 25000.0

#: Met to sample: the paths of ARL files (or one path), or one Dataset in
#: :func:`arlmet.open_dataset`'s layout, such as a converter's output.
Met = str | os.PathLike[str] | Iterable[str | os.PathLike[str]] | xr.Dataset


@contextmanager
def _opened(met: Met) -> Iterator[list[Any]]:
    """Yield *met* as :func:`arlmet.sample_points` reads it: the files opened (and closed afterwards), or the Dataset."""
    import arlmet

    if isinstance(met, xr.Dataset):
        yield [met]
        return
    if isinstance(met, (str, os.PathLike)):
        if Path(met).is_dir():
            raise ValueError(
                f"met is a folder, {met}. Give the met files a run read, such "
                "as particles_metadata(path).met_files."
            )
        met = [met]
    handles = [arlmet.File(path) for path in met]
    if not handles:
        raise ValueError("No met files were given.")
    try:
        yield handles
    finally:
        for handle in handles:
            handle.close()


def _met_times(sources: list[Any]) -> pd.DatetimeIndex:
    """Return the met times of the opened met, in order, in UTC without a time zone."""
    times: set[pd.Timestamp] = set()
    for source in sources:
        if isinstance(source, xr.Dataset):
            index = pd.DatetimeIndex(source.indexes["time"])
        else:
            index = pd.DatetimeIndex(source.times)
        if index.tz is not None:
            index = index.tz_convert("UTC").tz_localize(None)
        times.update(index)
    return pd.DatetimeIndex(sorted(times))


def _header(source: Any) -> Any:
    """Return what holds the grid, source and vertical axis of opened met: the file, or a Dataset's ``arl`` accessor."""
    return source.arl if isinstance(source, xr.Dataset) else source


def _met_variables(sources: list[Any]) -> set[str]:
    """
    Return the names of the variables every source of the opened met holds.

    A variable that only some of the files hold cannot be sampled at every
    met time, so it counts as missing.
    """
    held: list[set[str]] = []
    for source in sources:
        if isinstance(source, xr.Dataset):
            held.append({str(name) for name in source.data_vars})
        else:
            held.append({record.variable for record in source.records})
    return set.intersection(*held)


def _pressure_variable(sources: list[Any], names: set[str]) -> str | None:
    """
    Return what to sample for the pressure: the met's ``PRES`` field, arlmet's ``"pressure"``, or ``None``.

    A file with ``PRES`` on its levels, such as HRRR's, gives the pressure
    HYSPLIT reads. A file on pressure levels (GFS) or hybrid levels (ERA5)
    has none: its pressure comes from its vertical axis, which arlmet's
    virtual ``"pressure"`` reads. Terrain-following levels and WRF's hybrid
    levels give no pressure without ``PRES``, so met with such a file and
    no ``PRES`` in every file has none (``None``).
    """
    import arlmet

    if "PRES" in names:
        return "PRES"
    for source in sources:
        axis = _header(source).vertical_axis
        if isinstance(axis, arlmet.TerrainAxis | arlmet.WrfAxis):
            return None
    return "pressure"


def sample_met(
    particles: pd.DataFrame,
    met: Met,
    columns: Iterable[str] = ("mlht", "dens", "pres", "zsfc"),
    *,
    z_top: float | None = _DEFAULT_Z_TOP,
    message: str | os.PathLike[str] | None = None,
) -> pd.DataFrame:
    """
    Return the particles with met columns sampled at each particle's position.

    A transport model that writes only positions can get the columns
    HYSPLIT writes from the met this way. Each row is sampled at its
    ``lon``, ``lat``, ``zagl``, and ``time``: bilinear across the met's
    grid and linear in height between the internal levels of a HYSPLIT run
    with the top *z_top* (:func:`arlmet.sample_points`), then linear in time
    between the two met times around the particle's, as HYSPLIT does. Each column comes
    from these ARL variables:

    - ``mlht`` is ``PBLH``, the met model's boundary-layer height, in m
      above ground.
    - ``dens`` is 100 ``PRES`` / (R ``TEMP`` (1 + 0.61 ``SPHU``)), the
      density of moist air in kg m⁻³, with R = 287.04 J kg⁻¹ K⁻¹. Met
      without ``SPHU`` gives the density of dry air, up to about 1 %
      higher, as :func:`calc_pbl_height` uses.
    - ``pres`` is ``PRES``, the pressure, in hPa. Met without a ``PRES``
      field, such as GFS on pressure levels or ERA5 on model levels, gives
      the pressure of its vertical axis instead.
    - ``zsfc`` is ``SHGT``, the terrain height, in m above sea level.

    ``pres`` and ``dens`` follow what HYSPLIT reads, to the differences
    under Notes. ``mlht`` is the met's. HYSPLIT's own ``mlht`` is the met's
    ``PBLH`` (at least ``kmix0``) only when HYSPLIT runs with ``kmixd: 0``;
    with HYSPLIT's default, ``kmixd: 3``, it finds the mixed layer from the
    profile, as :func:`calc_pbl_height` does.

    Parameters
    ----------
    particles : pandas.DataFrame
        A particle table with ``lon``, ``lat``, ``zagl``, and ``time``
        (UTC; a time with a time zone is converted to UTC).
        ``sim.particles`` has ``time``. For a table a transport model
        returned, add it: the release time plus ``age`` minutes.
    met : path-like, iterable of path-like, or xarray.Dataset
        ARL met files that cover the particles' times, such as the files the
        run read (``met_files`` of :class:`runtraj.models.ModelRun` or of
        :func:`runtraj.particles.particles_metadata`), or met in another
        format as one Dataset in :func:`arlmet.open_dataset`'s layout
        (:meth:`runtraj.models.MetConfig.open`, or an arlmet converter's output).
    columns : iterable of str, default ("mlht", "dens", "pres", "zsfc")
        The columns to add.
    z_top : float or None, default 25000
        The top of the run's domain in meters (``z_top`` of
        :class:`runtraj.models.hysplit.HysplitConfig`), since HYSPLIT's
        internal levels depend on it. A stored run records it:
        ``particles_metadata(path).settings["z_top"]``. ``None`` reads the
        met's own levels, for a transport model that is not HYSPLIT; a row
        below their lowest or above their top is then NaN.
    message : path-like, optional
        The ``MESSAGE`` file of the run (HYSPLIT writes it in the run's
        working directory, which ``keep_workdir: true`` keeps). The internal levels
        are then the ones the run printed there, whatever its HYSPLIT
        version. Without it they are the ones HYSPLIT 5.4.0 and later pick
        for *z_top*; earlier versions pick a different grid when *z_top*
        is below about 1.1 km. Needs a *z_top*.

    Returns
    -------
    pandas.DataFrame
        A copy of *particles* with the columns added. A row off the met's
        grid is NaN.

    Raises
    ------
    ValueError
        If a column is not one of the four or is in *particles* already
        (drop it first), *particles* has no ``time`` column, a row is outside
        the files' times, *z_top* is not positive or is below a particle, or
        *message* has no internal grid line or is given without a *z_top*.
    KeyError
        If the met lacks a variable a column needs.

    Notes
    -----
    On two 6-hour HRRR runs from Salt Lake City, one in winter and one in
    summer, the sampled columns differ from the ones HYSPLIT wrote by:

    - ``zsfc``: a few meters, since HYSPLIT reads the terrain where the
      particle was before its turbulent step.
    - ``mlht``: a median of 16 to 30 m, against a HYSPLIT run with ``kmixd: 0``
      floored at ``kmix0``. HYSPLIT reads the nearest grid point's ``PBLH``;
      this function interpolates it.
    - ``pres``: a median of 0.02 hPa, 0.23 to 0.30 hPa at the 95th
      percentile, and 1.4 to 4.6 hPa at worst.
    - ``dens``: 0.17 to 0.57 % higher, more in moister air.

    Examples
    --------
    >>> meta = runtraj.particles.particles_metadata(
    ...     sim.particles_path
    ... )  # doctest: +SKIP
    >>> hysplit = sim.particles  # doctest: +SKIP
    >>> sampled = runtraj.sampling.sample_met(  # doctest: +SKIP
    ...     hysplit.drop(columns="pres"),
    ...     meta.met_files,
    ...     columns=["pres"],
    ...     z_top=meta.settings["z_top"],
    ... )
    >>> (sampled["pres"] - hysplit["pres"]).describe()  # doctest: +SKIP
    """
    import arlmet

    columns = list(dict.fromkeys(columns))
    unknown = [c for c in columns if c not in _MET_VARIABLES]
    if unknown:
        raise ValueError(
            f"sample_met cannot sample {unknown}; it samples {list(_MET_VARIABLES)}."
        )
    _check_z_top(particles, z_top)
    internal_grid = _read_internal_grid(message)
    present = [c for c in columns if c in particles.columns]
    if present:
        raise ValueError(
            f"The particles already have {present}. Drop them first to sample them."
        )
    if "time" not in particles.columns:
        raise ValueError(
            "sample_met needs a 'time' column: the release time plus 'age' minutes."
        )
    variables = list(dict.fromkeys(v for c in columns for v in _MET_VARIABLES[c]))

    with _opened(met) as sources:
        available = _met_variables(sources)
        if "SPHU" not in available:
            variables = [v for v in variables if v != "SPHU"]
        pressure = _pressure_variable(sources, available)
        missing = set(variables) - available - {"PRES"}
        if "PRES" in variables and pressure is None:
            missing.add("PRES")
        if missing:
            raise KeyError(
                f"sample_met needs the met variables {sorted(missing)}, which the "
                "met lacks."
            )
        # PRES is sampled as the pressure the met has (checked just above).
        names = [(pressure or v) if v == "PRES" else v for v in variables]
        met_times = _met_times(sources)
        times = _utc(pd.DatetimeIndex(particles["time"]))
        before, after, weight = _bracket(met_times, times)
        # Each row is sampled at the met time before it and, unless it is on
        # a met time, at the one after. One call samples both.
        later = weight > 0
        position = particles[["lon", "lat", "zagl"]].to_numpy(dtype=float)
        points = pd.DataFrame(
            np.concatenate([position, position[later]]), columns=["lon", "lat", "z"]
        )
        points["time"] = np.concatenate([before, after[later]])
        sampled = arlmet.sample_points(
            sources,
            points,
            names,
            z_kind="agl",
            model_top=z_top,
            internal_grid=internal_grid,
        )

    n = len(particles)
    w = weight[later]
    values_at: dict[str, np.ndarray] = {}
    for variable, name in zip(variables, names, strict=True):
        values = sampled[name].to_numpy(dtype=float)
        at_rows = values[:n].copy()
        at_rows[later] = (1.0 - w) * at_rows[later] + w * values[n:]
        values_at[variable] = at_rows

    out = particles.copy()
    for column in columns:
        if column == "dens":
            sphu = values_at.get("SPHU", 0.0)
            virtual_temperature = values_at["TEMP"] * (1.0 + 0.61 * sphu)
            out[column] = 100.0 * values_at["PRES"] / (_R_DRY * virtual_temperature)
        else:
            out[column] = values_at[_MET_VARIABLES[column][0]]
    return out


def _bracket(
    met_times: pd.DatetimeIndex, times: pd.DatetimeIndex
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Return the met times before and after each time, and the weight of the one after.

    A time on a met time has that time for both, and weight 0.
    """
    if len(met_times) == 0:
        raise ValueError("The met files hold no time steps.")
    # In nanoseconds both: a particle's time need not be a whole microsecond.
    met_times = met_times.as_unit("ns")
    times = pd.DatetimeIndex(times).as_unit("ns")
    after = met_times.searchsorted(times, side="left")
    outside = (after == len(met_times)) | np.asarray(times < met_times[0])
    if outside.any():
        raise ValueError(
            f"{int(outside.sum())} particle rows are outside the met files' times "
            f"({met_times[0]} to {met_times[-1]}): {times[outside].min()} "
            f"to {times[outside].max()}."
        )
    t1 = met_times[after]
    exact = np.asarray(t1 == times)
    t0 = met_times[np.where(exact, after, after - 1)]
    elapsed = (times - t0).to_numpy()
    span = np.where(exact, np.timedelta64(1, "s"), (t1 - t0).to_numpy())
    weight = np.where(exact, 0.0, elapsed / span)
    return t0.to_numpy(), t1.to_numpy(), weight.astype(float)


#: HYSPLIT's constants for the mixed layer (``stbcon.f``): gravity in m s⁻²,
#: von Kármán's constant, and the heat capacity of air in J kg⁻¹ K⁻¹.
_HYSPLIT_GRAV = 9.80616
_VON_KARMAN = 0.4
_CP = 1005.0
#: The top of HYSPLIT's surface layer, in m above ground (``metlvl.f``).
_SURFACE_LAYER_TOP = 75.0
#: The bulk Richardson number at the top of the mixed layer (``stbanl.f``).
_CRITICAL_RICHARDSON = 0.25

#: FLEXPART's constants for the mixed layer (``par_mod.f90`` and
#: ``richardson`` in ``getfields_mod.f90``): gravity in m s⁻², the gas
#: constant and heat capacity of dry air in J kg⁻¹ K⁻¹, the weight ``b`` of
#: u*² in the wind shear and ``bs`` of the temperature excess, and the
#: least and greatest mixed-layer height in m.
_FLEXPART_GRAV = 9.81
_FLEXPART_R_AIR = 287.05
_FLEXPART_CP = 1004.6
_FLEXPART_B = 100.0
_FLEXPART_BS = 8.5
_FLEXPART_HMIX_MIN = 100.0
_FLEXPART_HMIX_MAX = 4500.0

#: The met variables each method of :func:`calc_pbl_height` reads, besides
#: the pressure (``PRES`` or the vertical axis) the profile methods need.
#: ``hysplit`` also needs ``USTR`` or ``UMOF`` and ``VMOF``.
_PBL_VARIABLES: dict[str, set[str]] = {
    "hysplit": {"TEMP", "UWND", "VWND", "SHGT", "SHTF"},
    "hysplit_temperature": {"TEMP", "SHGT"},
    "met": {"PBLH", "SHGT"},
    "flexpart": {
        "TEMP",
        "SPHU",
        "UWND",
        "VWND",
        "PRSS",
        "T02M",
        "DP2M",
        "U10M",
        "V10M",
        "USTR",
        "SHTF",
    },
}


def calc_pbl_height(
    particles: pd.DataFrame,
    met: Met,
    *,
    method: str = "hysplit",
    z_top: float = _DEFAULT_Z_TOP,
    message: str | os.PathLike[str] | None = None,
) -> np.ndarray:
    """
    Return the mixed-layer height at each particle, found from the met as a transport model finds it.

    The mixed layer a stage reads should be the one the particles were
    mixed in, and each transport model finds it in its own way. Choose the
    *method* of the run the particles came from:

    - ``"hysplit"`` (the default): HYSPLIT's default, ``kmixd: 3``. Going
      up the column, the mixed layer ends where the bulk Richardson number
      between the lowest level and the level reached passes 0.25 (below).
    - ``"hysplit_temperature"``: HYSPLIT's ``kmixd: 1``. The top is the
      highest level whose potential temperature is within 2 K of the
      column's lowest.
    - ``"met"``: the met's own boundary-layer height ``PBLH``, as it is
      used with HYSPLIT's ``kmixd: 0``.
    - ``"flexpart"``: FLEXPART's Richardson method (below), for a FLEXPART
      run on ERA5 model levels. FLEXPART on GFS uses the met's ``PBLH``
      instead; give it ``"met"``.

    HYSPLIT finds the height at each met grid point and met time, and a
    particle takes the nearest grid point's height at the met times before
    and after it, linear in time. The three HYSPLIT methods do the same, on
    HYSPLIT's internal levels for a run with the top *z_top*. They apply
    no minimum, as HYSPLIT's ``kmix0`` does: take ``numpy.maximum`` with
    yours. Each height is at least a little above the lowest internal
    level, as in HYSPLIT.

    ``"hysplit"`` is ported from HYSPLIT's source (``stbanl.f``, Holtslag
    and Boville, 1993)::

        Ri = g · (θ(z) − θ₁ − θₑ) · (z − z₁) / ((θ₁ + θₑ) · |V(z) − V₁|²)

    with θ the potential temperature, V the horizontal wind, and level 1
    HYSPLIT's lowest internal level. θₑ, the temperature excess of rising
    air, is ``8.5 u* θ* / (u*³ + 0.6 w*³)^(1/3)`` in unstable air and 0
    otherwise, with the friction velocity ``u*`` (``USTR``) and the
    friction temperature ``θ*`` from the sensible heat flux (``SHTF``) at
    the ground. The height is interpolated to where Ri is 0.25. A column
    with no height where Ri reaches 0.25 gets the second-highest internal
    level, as in HYSPLIT.

    ``"flexpart"`` is ported from FLEXPART 11's ``richardson``
    (``getfields_mod.f90``, after Vogelezang and Holtslag, 1996). It works
    on the met's own levels, with heights integrated up from 2 m, and
    measures from the 2 m virtual potential temperature θᵥ₂ (``T02M``,
    ``DP2M``) and the wind of the lowest level V₁::

        Ri = g · (θᵥ(z) − θᵥ₂ − θₑ) · (z − 2) / ((θᵥ₂ + θₑ) · max(|V(z) − V₁|² + 100 u*², 0.1))

    The top is where Ri first passes 0.25 in a layer that is not unstable,
    found to a twentieth of the layer. In unstable air the temperature
    excess θₑ = 8.5 H / (cₚ w*), with H the upward heat flux and w* the
    convective velocity of that top, is added and the top found again, up
    to three times. The height is kept between 100 and 4500 m, as
    FLEXPART keeps it. A particle takes the greatest height of the four
    grid points around it at the met times before and after it, as
    FLEXPART does by default. FLEXPART's ``LSUBGRID: 1`` raises the height
    by a subgrid-orography excess; that is left out, since the converted
    met holds no subgrid orography, and FLEXPART leaves it off by default.

    On three 6-hour HRRR runs from Salt Lake City, ``"hysplit"``, kept at
    least 150 m, has a median ratio of 0.999 to 1.000 to the ``mlht``
    HYSPLIT wrote with ``kmixd: 3``, and 73 to 94 % of rows are within 2 %.
    On a 24-hour FLEXPART 11 run on ERA5 model levels from Salt Lake City,
    ``"flexpart"`` matched the mixed layer FLEXPART found (Notes).

    Parameters
    ----------
    particles : pandas.DataFrame
        A particle table with ``lon``, ``lat``, and ``time``.
    met : path-like, iterable of path-like, or xarray.Dataset
        ARL met files that cover the particles' times, or met in another
        format as one Dataset in :func:`arlmet.open_dataset`'s layout, as
        :func:`sample_met` takes. What it needs depends on *method*:

        - ``"hysplit"``: ``TEMP``, ``UWND``, ``VWND`` and ``SHGT``, the
          sensible heat flux ``SHTF``, the friction velocity ``USTR`` or the
          momentum fluxes ``UMOF`` and ``VMOF``, and the pressure:
          ``PRES``, or a vertical axis it follows from.
        - ``"hysplit_temperature"``: ``TEMP``, ``SHGT`` and the pressure.
        - ``"met"``: ``PBLH`` and ``SHGT``.
        - ``"flexpart"``: ``TEMP``, ``SPHU``, ``UWND``, ``VWND``, the
          pressure, and at the ground ``PRSS``, ``T02M``, ``DP2M``,
          ``U10M``, ``V10M``, ``USTR`` and ``SHTF``.
    method : {"hysplit", "hysplit_temperature", "met", "flexpart"}, default "hysplit"
        How the mixed layer is found: the transport model and its method.
    z_top : float, default 25000
        The top of the domain in meters, which sets HYSPLIT's internal
        levels (``z_top`` of :class:`runtraj.models.hysplit.HysplitConfig`).
        ``"flexpart"`` reads the met's own levels and ignores it.
    message : path-like, optional
        The ``MESSAGE`` file of the HYSPLIT run: the internal levels are
        then the ones it printed, whatever its version, as in
        :func:`sample_met`. Without it they are the ones HYSPLIT 5.4.0 and
        later pick. ``"flexpart"`` ignores it.

    Returns
    -------
    numpy.ndarray
        The mixed-layer height at each row, in m above ground. A row off
        the met's grid is NaN. A met time that lacks the surface fluxes,
        as a GFS analysis (``f000``) does without the previous cycle's
        forecast, has no height from ``"hysplit"`` or ``"flexpart"``: a row
        at that time is NaN, and a row between it and a met time with the
        fluxes takes the height of the other time. (HYSPLIT refuses such
        met, so there is no ``mlht`` of its own to compare.) A column where
        ``"flexpart"`` finds no top in the met's levels has none, where
        FLEXPART stops.

    Raises
    ------
    ValueError
        If *method* is not one of the four, *particles* has no ``time``
        column, a row is outside the files' times, or *message* has no
        internal grid line.
    KeyError
        If the met lacks a variable the method needs.

    Notes
    -----
    A Dataset gives the met's own values. HYSPLIT reads the ARL file
    written from it, whose values are packed: in mountains ``SHGT`` can
    move by a few meters. In a deep, nearly neutral afternoon layer, a few
    meters near the ground move the ``"hysplit"`` height by hundreds of
    meters, most of all on pressure levels, whose first level above the
    ground can be only meters above HYSPLIT's lowest internal level. On two
    hours of ERA5 pressure levels over Salt Lake City in July, the height
    from the Dataset had a median ratio of 0.91 to HYSPLIT's ``mlht``. From
    the ARL file HYSPLIT read, the median ratio was 0.999, with 99 % of rows
    within 2 %. To compare with a HYSPLIT run, give the files it read.

    FLEXPART writes a particle's ``hmix`` interpolated from the grid
    points' heights (bilinear across the grid, linear in time), not the
    greatest of them it mixes the particle in, so ``"flexpart"`` is at
    least the ``hmix`` it wrote at most rows. Interpolated the same way, the
    grid points' heights ``"flexpart"`` finds had a median ratio of 1.000
    to the ``hmix`` of the run above, with 80 % of 24,000 rows within 2 %
    and 94 % within 10 %. The rest comes from the converted met, which
    differs from what FLEXPART reads in two small ways: an ARL hybrid level
    keeps its pressure offset to the whole hPa, and ``USTR`` is found with
    the density of dry air at 2 m, where FLEXPART uses moist air.

    Examples
    --------
    >>> meta = runtraj.particles.particles_metadata(
    ...     sim.particles_path
    ... )  # doctest: +SKIP
    >>> mlht = runtraj.sampling.calc_pbl_height(
    ...     sim.particles, meta.met_files
    ... )  # doctest: +SKIP

    A FLEXPART run on ERA5, with the met its transport model opens:

    >>> met = met_config.open(files, bounds)  # doctest: +SKIP
    >>> mlht = runtraj.sampling.calc_pbl_height(
    ...     particles, met, method="flexpart"
    ... )  # doctest: +SKIP
    """
    if method not in _PBL_VARIABLES:
        raise ValueError(
            f"calc_pbl_height has no method {method!r}; choose one of "
            f"{list(_PBL_VARIABLES)}."
        )
    if "time" not in particles.columns:
        raise ValueError(
            "calc_pbl_height needs a 'time' column: the release time plus 'age' minutes."
        )
    _check_z_top(particles, z_top)
    internal_grid = _read_internal_grid(message)
    with _opened(met) as sources:
        names = _met_variables(sources)
        pressure = _pressure_variable(sources, names)
        needed = set(_PBL_VARIABLES[method])
        if method == "hysplit":
            needed |= {"USTR"} if "USTR" in names else {"UMOF", "VMOF"}
        missing = needed - names
        if method != "met" and pressure is None:
            missing.add("PRES")
        if missing:
            raise KeyError(
                f"calc_pbl_height needs the met variables {sorted(missing)}, which "
                "the met lacks."
            )
        met_times = _met_times(sources)
        before, after, weight = _bracket(
            met_times, _utc(pd.DatetimeIndex(particles["time"]))
        )
        header = _header(sources[0])
        reads = _grid_points_read(
            header.grid,
            particles["lon"].to_numpy(dtype=float),
            particles["lat"].to_numpy(dtype=float),
            before,
            after,
            weight,
            around=method == "flexpart",
        )
        key = ["i", "j", "time"]
        columns = reads[[*key, "lon", "lat"]].drop_duplicates(subset=key)
        columns = columns.reset_index(drop=True)
        if method == "flexpart":
            zmix = _flexpart_columns(sources, columns, header, str(pressure))
        else:
            zmix = _hysplit_columns(
                sources,
                columns,
                header,
                names,
                pressure,
                method,
                z_top,
                internal_grid,
            )
    columns["zmix"] = zmix
    reads = reads.merge(columns[[*key, "zmix"]], on=key, how="left")

    n = len(particles)
    if method == "flexpart":
        # The greatest height of the grid points around, at both met times.
        most = reads.groupby("row")["zmix"].max()
        return most.reindex(range(n)).to_numpy(dtype=float)

    # Each row takes its grid point's height at the met times around it.
    out = np.full(n, np.nan)
    after_value = np.full(n, np.nan)
    first = ~reads["late"].to_numpy()
    out[reads["row"].to_numpy()[first]] = reads["zmix"].to_numpy()[first]
    after_value[reads["row"].to_numpy()[~first]] = reads["zmix"].to_numpy()[~first]
    later = weight > 0
    w = weight[later]
    before_value, after_value = out[later], after_value[later]
    # Between a met time with a height and one without (a GFS analysis has
    # no fluxes), the row takes the height it has.
    out[later] = np.where(
        np.isnan(before_value),
        after_value,
        np.where(
            np.isnan(after_value),
            before_value,
            (1.0 - w) * before_value + w * after_value,
        ),
    )
    return out


def _grid_points_read(
    grid: Any,
    lon: np.ndarray,
    lat: np.ndarray,
    before: np.ndarray,
    after: np.ndarray,
    weight: np.ndarray,
    *,
    around: bool,
) -> pd.DataFrame:
    """
    Return the met grid points and times each row reads its height from.

    One table row per read: the particle's ``row``, the grid point's ``i``,
    ``j``, ``lon`` and ``lat``, the met ``time``, and ``late`` for the met
    time after the particle's. A row reads the nearest grid point, or with
    *around* the four around it, at the met time before it and, unless it
    is on a met time, the one after. A grid point off the grid is not read.
    """
    x, y = grid.fractional_indices(lon, lat)
    if around:
        x0, y0 = np.floor(x), np.floor(y)
        corners = [(x0, y0), (x0 + 1, y0), (x0, y0 + 1), (x0 + 1, y0 + 1)]
    else:
        corners = [(np.rint(x), np.rint(y))]
    rows = np.arange(len(lon))
    later = weight > 0
    parts = []
    for i, j in corners:
        for late, keep, time in (
            (False, rows, before),
            (True, rows[later], after[later]),
        ):
            parts.append(
                pd.DataFrame(
                    {
                        "row": keep,
                        "i": i[keep],
                        "j": j[keep],
                        "time": time,
                        "late": late,
                    }
                )
            )
    reads = pd.concat(parts, ignore_index=True)
    if grid.wraps_lon:
        reads["i"] = reads["i"] % grid.nx
    on_grid = (
        reads["i"].between(0, grid.nx - 1) & reads["j"].between(0, grid.ny - 1)
    ).to_numpy()
    reads = reads[on_grid].reset_index(drop=True)
    i = reads["i"].to_numpy(dtype=int)
    j = reads["j"].to_numpy(dtype=int)
    coords = grid.calculate_coords()
    lons, lats = coords["lon"][1], coords["lat"][1]
    if lons.ndim == 1:
        reads["lon"], reads["lat"] = lons[i], lats[j]
    else:
        reads["lon"], reads["lat"] = lons[j, i], lats[j, i]
    return reads


def _hysplit_columns(
    sources: list[Any],
    columns: pd.DataFrame,
    header: Any,
    names: set[str],
    pressure: str | None,
    method: str,
    z_top: float,
    internal_grid: tuple[int, float, float, float] | None,
) -> np.ndarray:
    """Return the mixed-layer height of each grid point and met time in *columns*, as HYSPLIT's *method* finds it."""
    import arlmet

    momentum = ["USTR"] if "USTR" in names else ["UMOF", "VMOF"]
    ground_variables = {
        "hysplit": ["SHGT", "SHTF", *momentum],
        "hysplit_temperature": ["SHGT"],
        "met": ["SHGT", "PBLH"],
    }[method]
    levels = arlmet.internal_levels(
        sources[0], model_top=z_top, internal_grid=internal_grid
    )
    ground = arlmet.sample_points(
        sources,
        columns[["lon", "lat", "time"]].assign(z=0.0),
        ground_variables,
        z_kind="agl",
        method="nearest",
        model_top=z_top,
        internal_grid=internal_grid,
    )
    # The internal levels above each column's ground, (columns, levels).
    zz = levels.heights_at(ground["SHGT"].to_numpy(dtype=float))
    n_col, n_lev = zz.shape
    # Always a little above the lowest level, as in HYSPLIT.
    floor = zz[:, 0] + (zz[:, 1] - zz[:, 0]) / 1000.0 if n_lev >= 2 else zz[:, 0]
    if method == "met":
        return np.maximum(ground["PBLH"].to_numpy(dtype=float), floor)

    at_levels = pd.DataFrame(
        {
            "lon": np.repeat(columns["lon"].to_numpy(), n_lev),
            "lat": np.repeat(columns["lat"].to_numpy(), n_lev),
            "time": np.repeat(columns["time"].to_numpy(), n_lev),
            "z": zz.ravel(),
        }
    )
    profile_variables = ["TEMP", str(pressure)]
    if method == "hysplit":
        profile_variables += ["UWND", "VWND"]
        if "SPHU" in names:
            profile_variables.append("SPHU")
    profile = arlmet.sample_points(
        sources,
        at_levels,
        profile_variables,
        z_kind="agl",
        method="nearest",
        model_top=z_top,
        internal_grid=internal_grid,
    )

    def grid(name: str) -> np.ndarray:
        return profile[name].to_numpy(dtype=float).reshape(n_col, n_lev)

    temp, pres = grid("TEMP"), grid(str(pressure))
    theta = temp * (1000.0 / pres) ** 0.286
    if method == "hysplit_temperature":
        return np.maximum(_temperature_mixed_layer(zz, theta), floor)

    uu, vv = grid("UWND"), grid("VWND")
    sphu = grid("SPHU") if "SPHU" in profile_variables else np.zeros_like(temp)
    density = 100.0 * pres / (_R_DRY * temp * (1.0 + 0.61 * sphu))
    ks = max(int(np.sum(levels.heights <= _SURFACE_LAYER_TOP)), 1) - 1
    if "USTR" in momentum:
        ustar = np.maximum(ground["USTR"].to_numpy(dtype=float), 0.0001)
    else:
        # u* from the momentum fluxes, as stbanl does without USTR, with the
        # density of its lowest level above the first.
        def component(flux: str) -> np.ndarray:
            f = ground[flux].to_numpy(dtype=float)
            rho = density[:, min(max(1, ks), n_lev - 1)]
            return np.sign(f) * np.maximum(0.01, np.sqrt(np.abs(f) / rho))

        ustar = np.hypot(component("UMOF"), component("VMOF"))
    heat = ground["SHTF"].to_numpy(dtype=float)
    if header.source.strip() == "ETA":
        heat = -heat  # NAM's (ETA's) flux is positive downward (prfcom.f)
    zmix = _richardson_mixed_layer(zz, theta, uu, vv, density, ustar, heat, ks)
    # Without the fluxes the stability is unknown: no height, rather than
    # the stable one a missing heat flux would give.
    zmix[~(np.isfinite(heat) & np.isfinite(ustar))] = np.nan
    return zmix


def _temperature_mixed_layer(zz: np.ndarray, theta: np.ndarray) -> np.ndarray:
    """
    Return the mixed-layer height of each column, as HYSPLIT's ``stbanl.f`` finds it for ``kmixd: 1``.

    *zz* (m above ground) and *theta* (potential temperature, K) are
    ``(columns, levels)``. Going down from the top, the mixed layer ends at
    the first level whose potential temperature is within 2 K of the
    column's lowest.
    """
    n_lev = zz.shape[1]
    within = theta <= np.nanmin(theta, axis=1, keepdims=True) + 2.0
    top = n_lev - 1 - np.argmax(within[:, ::-1], axis=1)
    return np.take_along_axis(zz, top[:, None], axis=1)[:, 0]


def _flexpart_columns(
    sources: list[Any], columns: pd.DataFrame, header: Any, pressure: str
) -> np.ndarray:
    """Return the mixed-layer height of each grid point and met time in *columns*, as FLEXPART's ``richardson`` finds it."""
    import arlmet

    ground_variables = ["PRSS", "T02M", "DP2M", "U10M", "V10M", "USTR", "SHTF"]
    ground = arlmet.sample_points(
        sources,
        columns[["lon", "lat", "time"]].assign(z=0.0),
        ground_variables,
        z_kind="native",
        method="nearest",
    )
    # The met's own levels; level 0 is the ground.
    n_lev = len(header.vertical_axis.levels) - 1
    at_levels = pd.DataFrame(
        {
            "lon": np.repeat(columns["lon"].to_numpy(), n_lev),
            "lat": np.repeat(columns["lat"].to_numpy(), n_lev),
            "time": np.repeat(columns["time"].to_numpy(), n_lev),
            "z": np.tile(np.arange(1.0, n_lev + 1.0), len(columns)),
        }
    )
    variables = ["TEMP", "SPHU", "UWND", "VWND", pressure]
    profile = arlmet.sample_points(
        sources, at_levels, variables, z_kind="native", method="nearest"
    )

    def grid(name: str) -> np.ndarray:
        return profile[name].to_numpy(dtype=float).reshape(len(columns), n_lev)

    def at_ground(name: str) -> np.ndarray:
        return ground[name].to_numpy(dtype=float)

    heat = at_ground("SHTF")
    if header.source.strip() == "ETA":
        heat = -heat  # positive downward in NAM's (ETA's) files
    zmix = _flexpart_mixed_layer(
        100.0 * grid(pressure),
        grid("TEMP"),
        grid("SPHU"),
        grid("UWND"),
        grid("VWND"),
        100.0 * at_ground("PRSS"),
        at_ground("T02M"),
        at_ground("DP2M"),
        at_ground("U10M"),
        at_ground("V10M"),
        at_ground("USTR"),
        heat,
    )
    zmix[~(np.isfinite(heat) & np.isfinite(at_ground("USTR")))] = np.nan
    return zmix


def _saturation_vapor_pressure(t: np.ndarray) -> np.ndarray:
    """Return the saturation vapor pressure over water in Pa at *t* in K, by Goff and Gratch, as FLEXPART's ``ew``."""
    y = 373.16 / t
    a = -7.90298 * (y - 1.0) + 5.02808 * 0.43429 * np.log(y)
    c = -1.3816 * (-1.0 + 10.0 ** ((1.0 - 1.0 / y) * 11.344)) / 1e7
    d = 8.1328 * (-1.0 + 10.0 ** ((1.0 - y) * 3.49149)) / 1e3
    return 101324.6 * 10.0 ** (a + c + d)


def _flexpart_mixed_layer(
    pres: np.ndarray,
    temp: np.ndarray,
    sphu: np.ndarray,
    uu: np.ndarray,
    vv: np.ndarray,
    psfc: np.ndarray,
    t2: np.ndarray,
    td2: np.ndarray,
    u10: np.ndarray,
    v10: np.ndarray,
    ustar: np.ndarray,
    heat: np.ndarray,
) -> np.ndarray:
    """
    Return the mixed-layer height of each column, as FLEXPART's ``richardson`` finds it.

    *pres* (Pa), *temp* (K), *sphu* (kg kg⁻¹), *uu* and *vv* (m s⁻¹) are
    ``(columns, levels)`` on the met's levels from the lowest up; *psfc*
    (Pa), *t2* and *td2* (K), *u10* and *v10* (m s⁻¹), *ustar* (m s⁻¹) and
    *heat*, the sensible heat flux (W m⁻², positive upward), are per
    column. A level at or below the ground (its pressure at least
    *psfc*) is passed over. A column with no top is NaN.
    """
    g, cp = _FLEXPART_GRAV, _FLEXPART_CP
    kappa = _FLEXPART_R_AIR / cp
    n_col = pres.shape[0]
    # The levels above the ground first in each column; the rest are NaN.
    above = pres < psfc[:, None]
    order = np.argsort(~above, axis=1, kind="stable")

    def compact(a: np.ndarray) -> np.ndarray:
        return np.where(
            np.take_along_axis(above, order, axis=1),
            np.take_along_axis(a, order, axis=1),
            np.nan,
        )

    pres, temp, sphu, uu, vv = (compact(a) for a in (pres, temp, sphu, uu, vv))
    tv = temp * (1.0 + 0.608 * sphu)
    theta = tv * (100000.0 / pres) ** kappa

    # Heights, integrated up from 2 m with the virtual temperature.
    tv2 = t2 * (1.0 + 0.378 * _saturation_vapor_pressure(td2) / psfc)
    const = _FLEXPART_R_AIR / g
    z = np.empty_like(pres)
    pold, tvold, zold = psfc, tv2, np.full(n_col, 2.0)
    for k in range(pres.shape[1]):
        log_p = np.log(pold / pres[:, k])
        with np.errstate(divide="ignore", invalid="ignore"):
            layer = np.where(
                np.abs(tv[:, k] - tvold) > 0.2,
                const * log_p * (tv[:, k] - tvold) / np.log(tv[:, k] / tvold),
                const * log_p * tv[:, k],
            )
        z[:, k] = zold + layer
        pold, tvold, zold = pres[:, k], tv[:, k], z[:, k]

    # Each layer's lower edge: the 2 m level below the lowest model level.
    shear = _FLEXPART_B * ustar**2
    u_below = np.column_stack([u10, uu[:, :-1]])
    v_below = np.column_stack([v10, vv[:, :-1]])
    z_below = np.column_stack([np.full(n_col, 2.0), z[:, :-1]])
    rows = np.arange(n_col)

    def richardson(theta_at, z_at, u_at, v_at, thetaref):
        du = (u_at - uu[:, :1]) ** 2 + (v_at - vv[:, :1]) ** 2 + shear[:, None]
        return g / thetaref * (theta_at - thetaref) * (z_at - 2.0) / np.maximum(du, 0.1)

    excess = np.zeros(n_col)
    h = np.full(n_col, np.nan)
    going = np.ones(n_col, dtype=bool)
    for _ in range(3):
        thetaref = (tv2 * (100000.0 / psfc) ** kappa + excess)[:, None]
        theta_below = np.column_stack([thetaref[:, 0], theta[:, :-1]])
        ri = richardson(theta, z, uu, vv, thetaref)
        # The first level where Ri passes 0.25 above a layer that is not unstable.
        top = (ri > _CRITICAL_RICHARDSON) & (theta_below < theta)
        found = top.any(axis=1)
        k = np.argmax(top, axis=1)
        # Then the first twentieth of that layer where it passes.
        frac = np.arange(1, 21) / 20.0
        zl = z_below[rows, k][:, None] + frac * (z[rows, k] - z_below[rows, k])[:, None]
        thetal = (
            theta_below[rows, k][:, None]
            + frac * (theta[rows, k] - theta_below[rows, k])[:, None]
        )
        ul = (
            u_below[rows, k][:, None] + frac * (uu[rows, k] - u_below[rows, k])[:, None]
        )
        vl = (
            v_below[rows, k][:, None] + frac * (vv[rows, k] - v_below[rows, k])[:, None]
        )
        ril = richardson(thetal, zl, ul, vl, thetaref)
        crossed = ril > _CRITICAL_RICHARDSON
        step = np.where(crossed.any(axis=1), np.argmax(crossed, axis=1), 19)
        new_h = np.where(found, zl[rows, step], np.nan)
        h = np.where(going, new_h, h)
        # In unstable air, again with the temperature excess of rising air.
        unstable = going & found & (heat > 0)
        with np.errstate(invalid="ignore"):
            wst = (h * g / thetaref[:, 0] * heat / cp) ** 0.333
            excess = np.where(unstable, _FLEXPART_BS * heat / cp / wst, excess)
        going = unstable
        if not going.any():
            break
    return np.clip(h, _FLEXPART_HMIX_MIN, _FLEXPART_HMIX_MAX)


def _richardson_mixed_layer(
    zz: np.ndarray,
    theta: np.ndarray,
    uu: np.ndarray,
    vv: np.ndarray,
    density: np.ndarray,
    ustar: np.ndarray,
    heat: np.ndarray,
    ks: int,
) -> np.ndarray:
    """
    Return the mixed-layer height of each column, as HYSPLIT's ``stbanl.f`` finds it for ``kmixd: 3``.

    *zz* (m above ground), *theta* (potential temperature, K), *uu* and
    *vv* (m s⁻¹) and *density* (kg m⁻³) are ``(columns, levels)`` on the
    internal levels; *ustar* (m s⁻¹) and *heat*, the sensible heat flux
    (W m⁻², positive upward), are per column; *ks* is the index of the top
    of the surface layer. The stability at the top of the surface layer
    comes from the fluxes, and the temperature excess from it in unstable
    air.
    """
    n_col, n_lev = zz.shape
    heat = np.maximum(-25.0, heat)
    tstar = np.sign(-heat) * np.maximum(
        0.0001, np.abs(heat / (density[:, ks] * _CP * ustar))
    )
    zl = zz[:, ks] * _VON_KARMAN * _HYSPLIT_GRAV * tstar / (ustar**2 * theta[:, ks])
    unstable = np.clip(zl, -2.0, 15.0) < 0

    # Up the column until Ri passes 0.25, interpolating to where it is 0.25.
    zmix = zz[:, 0].copy()
    ri = np.zeros(n_col)
    done = np.zeros(n_col, dtype=bool)
    for k in range(1, n_lev):
        go = ~done & (ri < _CRITICAL_RICHARDSON)
        done |= ~go
        if not go.any():
            break
        tstk = tstar * density[:, ks] / density[:, k]
        wstar = np.abs(_HYSPLIT_GRAV * ustar * tstk * zz[:, k] / theta[:, 0]) ** (1 / 3)
        excess = np.where(
            unstable,
            np.abs(8.5 * ustar * tstk / (ustar**3 + 0.6 * wstar**3) ** (1 / 3)),
            0.0,
        )
        t1 = theta[:, 0] + excess
        du = np.maximum(0.0001, (uu[:, k] - uu[:, 0]) ** 2 + (vv[:, k] - vv[:, 0]) ** 2)
        old = ri
        new = _HYSPLIT_GRAV * (theta[:, k] - t1) * (zz[:, k] - zz[:, 0]) / t1 / du
        with np.errstate(divide="ignore", invalid="ignore"):
            crossed = (zz[:, k] - zz[:, k - 1]) / (new - old) * (
                _CRITICAL_RICHARDSON - old
            ) + zz[:, k - 1]
        zmix = np.where(go, np.where(new == old, zz[:, k], crossed), zmix)
        ri = np.where(go, new, ri)
    if n_lev >= 2:
        # No crossing below the top: the second-highest level, as in HYSPLIT;
        # and always a little above the lowest level.
        zmix = np.minimum(zmix, zz[:, -2])
        zmix = np.maximum(zmix, zz[:, 0] + (zz[:, 1] - zz[:, 0]) / 1000.0)
    return zmix


__all__ = [
    "calc_pbl_height",
    "nearest_cell",
    "sample_field",
    "sample_met",
    "vertical_dim",
]

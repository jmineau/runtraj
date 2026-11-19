"""
The GRIB files of a FLEXPART met: their valid times, the ones a run needs, and the fields in them.
"""

from __future__ import annotations

import datetime as dt
import functools
import os
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import xarray as xr

from runtraj.exceptions import MeteorologyError

if TYPE_CHECKING:
    from runtraj.spatial import Bounds

#: Grid cells of met kept around the particles when GRIB files are opened for
#: them: interpolation at a particle near the edge reads the cells beyond it.
MARGIN_CELLS = 3


def _first_message_keys(path: str | Path, keys: tuple[str, ...]) -> list:
    """Return *keys* of the first GRIB message in *path*, read with eccodes."""
    try:
        import eccodes
    except ImportError as err:
        raise ImportError(
            'FLEXPART runs read GRIB met with eccodes: pip install "runtraj[flexpart]"'
        ) from err
    with open(path, "rb") as f:
        handle = eccodes.codes_grib_new_from_file(f)
        if handle is None:
            raise ValueError(f"{path} holds no GRIB message.")
        try:
            return [eccodes.codes_get(handle, key) for key in keys]
        finally:
            eccodes.codes_release(handle)


def grib_valid_time(path: str | Path) -> dt.datetime:
    """Return the valid time of the first GRIB message in *path*."""
    date, time = _first_message_keys(path, ("validityDate", "validityTime"))
    return dt.datetime.strptime(f"{date:08d}{time:04d}", "%Y%m%d%H%M")


def grib_grid(path: str | Path) -> str:
    """Return the grid of the first GRIB message in *path*, such as ``0.5x0.5 deg, 720x361``."""
    di, dj, ni, nj = _first_message_keys(
        path,
        (
            "iDirectionIncrementInDegrees",
            "jDirectionIncrementInDegrees",
            "Ni",
            "Nj",
        ),
    )
    return f"{di:g}x{dj:g} deg, {ni}x{nj}"


def nxshift(source: str, path: str | Path) -> int:
    """
    Return FLEXPART's ``NXSHIFT`` for the files of *source*, from the first one at *path*.

    FLEXPART stops unless it is 0 on a grid that does not go round the
    earth. On a global grid it is 359 for ECMWF data, FLEXPART's own
    default, and 0 for GFS, which starts at 0° E.
    """
    if source != "era5":
        return 0
    di, ni = _first_message_keys(path, ("iDirectionIncrementInDegrees", "Ni"))
    return 359 if abs(di * ni - 360.0) < 1e-6 else 0


@functools.lru_cache(maxsize=16)
def _catalog(files: tuple[tuple[str, int, int], ...]) -> dict[dt.datetime, Path]:
    """Return the files by valid time; *files* are (path, size, mtime) so a changed file is read again."""
    by_time: dict[dt.datetime, Path] = {}
    for name, _, _ in files:
        valid = grib_valid_time(name)
        if valid in by_time:
            raise MeteorologyError(
                f"{by_time[valid].name} and {Path(name).name} are both for {valid}."
            )
        by_time[valid] = Path(name)
    return dict(sorted(by_time.items()))


def catalog(directory: str | Path, pattern: str = "*") -> dict[dt.datetime, Path]:
    """
    Return the GRIB files in *directory* that match *pattern*, by valid time.

    Each file is read once per process, and again only if it changes.

    Raises
    ------
    MeteorologyError
        If two files have the same valid time.
    """
    paths = sorted(p for p in Path(directory).glob(pattern) if p.is_file())
    key = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in paths)
    return _catalog(key)


def files_for(
    available: dict[dt.datetime, Path], window: tuple[dt.datetime, dt.datetime]
) -> dict[dt.datetime, Path]:
    """
    Return the files a run over *window* reads: from the last at or before its start to the first at or after its end.

    FLEXPART interpolates in time between files, so it needs one at or
    beyond each end.

    Raises
    ------
    MeteorologyError
        If no file is at or before the start or at or after the end, or two
        files in between are further apart than the closest two files in
        the directory (a missing file).
    """
    start, end = window
    times = list(available)
    before = [t for t in times if t <= start]
    after = [t for t in times if t >= end]
    if not before or not after:
        span = f"{times[0]} to {times[-1]}" if times else "no files"
        raise MeteorologyError(
            f"The GRIB files ({span}) do not cover the run from {start} to {end}."
        )
    chosen = [t for t in times if before[-1] <= t <= after[0]]
    step = min((b - a for a, b in zip(times, times[1:], strict=False)), default=None)
    gaps = [b - a for a, b in zip(chosen, chosen[1:], strict=False)]
    if step is not None and gaps and max(gaps) > step:
        raise MeteorologyError(
            f"The GRIB files for the run from {start} to {end} have a gap of "
            f"{max(gaps)}, where the closest are {step} apart: a file is missing."
        )
    return {t: available[t] for t in chosen}


def open_gfs(
    files: Iterable[str | os.PathLike[str]], bounds: Bounds | None = None
) -> xr.Dataset:
    """
    Return GFS GRIB2 *files* as one Dataset in :func:`arlmet.open_dataset`'s layout, cropped to *bounds*.

    Each file is opened with cfgrib and converted by
    :func:`arlmet.converters.from_gfs`, which reads only the cells inside
    *bounds* plus :data:`MARGIN_CELLS` cells on each side, so a global
    0.25 degree file costs memory only for the region the particles are
    in. ``None`` keeps the whole grid. The planetary boundary layer height
    (``HPBL``, NCEP's own parameter) is opened from each file on its own
    and comes through as ``PBLH``; a file without it has no ``PBLH``.

    Notes
    -----
    cfgrib writes an index file (``.idx``) beside each GRIB file the first
    time it opens it, and reads it on later opens; without it, every open
    reads the whole file again. Where the directory is not writable cfgrib
    warns and goes on without one.

    Raises
    ------
    ImportError
        If cfgrib is not installed (the ``flexpart`` extra).
    ValueError
        If *bounds* holds no cell of the files' grid.
    """
    try:
        import cfgrib
        from arlmet.converters import GFS_HPBL, from_gfs
    except ImportError as err:
        raise ImportError(
            "Opening GRIB met for the particles needs cfgrib: "
            'pip install "runtraj[flexpart]"'
        ) from err
    hpbl = {"filter_by_keys": GFS_HPBL, "read_keys": list(GFS_HPBL)}
    datasets = []
    for path in files:
        datasets.extend(cfgrib.open_datasets(str(path)))
        # HPBL opened on its own: in a forecast file it shares a level with
        # another field ecCodes has no name for, and open_datasets keeps one.
        boundary_layer = xr.open_dataset(path, engine="cfgrib", backend_kwargs=hpbl)
        if boundary_layer.data_vars:
            datasets.append(boundary_layer)
    if not datasets:
        raise ValueError("No GRIB files to open.")
    bbox = None
    if bounds is not None:
        bbox = _bbox(bounds, _cell_size(datasets[0]))
    return from_gfs(datasets, bbox=bbox)


def _cell_size(ds: xr.Dataset) -> float:
    """Return the larger of the grid's latitude and longitude spacings, in degrees."""
    steps = [
        float(np.abs(np.diff(ds[name].values[:2])).max(initial=0.0))
        for name in ("latitude", "longitude")
        if ds[name].size > 1
    ]
    return max(steps, default=0.0)


def _bbox(bounds: Bounds, cell: float) -> tuple[float, float, float, float]:
    """
    Return *bounds* widened by :data:`MARGIN_CELLS` cells of size *cell*, as ``(west, south, east, north)``.

    A box that would go round the earth is the whole circle of longitudes,
    and the latitudes stop at the poles.
    """
    margin = MARGIN_CELLS * cell
    width = (bounds.xmax - bounds.xmin) % 360.0
    if bounds.xmax - bounds.xmin >= 360.0 or width + 2 * margin >= 360.0:
        west, east = 0.0, 360.0
    else:
        west, east = bounds.xmin - margin, bounds.xmax + margin
    south = max(bounds.ymin - margin, -90.0)
    north = min(bounds.ymax + margin, 90.0)
    return (west, south, east, north)


__all__ = [
    "MARGIN_CELLS",
    "catalog",
    "files_for",
    "grib_grid",
    "grib_valid_time",
    "nxshift",
    "open_gfs",
]

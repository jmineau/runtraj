"""
The GRIB files of a FLEXPART met: their valid times, the ones a run needs, and the fields in them.
"""

from __future__ import annotations

import datetime as dt
import functools
import os
from collections.abc import Iterable
from pathlib import Path

import xarray as xr

from runtraj.exceptions import MeteorologyError


def _eccodes():
    """Return the eccodes module, or say which extra installs it."""
    try:
        import eccodes
    except ImportError as err:
        raise ImportError(
            'FLEXPART runs read GRIB met with eccodes: pip install "runtraj[flexpart]"'
        ) from err
    return eccodes


def _first_message_keys(path: str | Path, keys: tuple[str, ...]) -> list:
    """Return *keys* of the first GRIB message in *path*, read with eccodes."""
    eccodes = _eccodes()
    with open(path, "rb") as f:
        handle = eccodes.codes_grib_new_from_file(f)
        if handle is None:
            raise ValueError(f"{path} holds no GRIB message.")
        try:
            return [eccodes.codes_get(handle, key) for key in keys]
        finally:
            eccodes.codes_release(handle)


def holds_grib(path: str | Path) -> bool:
    """
    Return whether *path* holds a GRIB message.

    A met directory often holds other files: cfgrib writes an index
    (``.idx``) beside each GRIB file it opens, and ``pattern`` is ``*``
    unless set.
    """
    eccodes = _eccodes()
    try:
        _first_message_keys(path, ())
    except (ValueError, eccodes.CodesInternalError):
        # No message at all, or bytes eccodes cannot read as one.
        return False
    return True


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
        if not holds_grib(name):
            continue
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

    A file that holds no GRIB message, such as an index cfgrib wrote, is
    left out. Each file is read once per process, and again only if it
    changes.

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


def open_era5(files: Iterable[str | os.PathLike[str]]) -> xr.Dataset:
    """
    Return flex_extract's ERA5 *files* as one Dataset in :func:`arlmet.open_dataset`'s layout.

    Each file is opened with cfgrib and the lot converted by
    :func:`arlmet.converters.from_flex_extract`, which puts the fields that
    flex_extract writes on two longitude conventions on one, reads its
    fluxes as the rates they are, and finds the friction velocity from the
    surface stress as FLEXPART does. The files hold no boundary layer
    height, so the Dataset has no ``PBLH``.

    Notes
    -----
    The whole grid is kept: flex_extract's files are already cropped to the
    domain they were retrieved for, and the converter reads every field into
    memory.

    Raises
    ------
    ImportError
        If cfgrib is not installed (the ``flexpart`` extra).
    ValueError
        If there are no files, or the converter refuses them (no model
        levels, no surface pressure, grids that differ).
    """
    try:
        import cfgrib
        from arlmet.converters import from_flex_extract
    except ImportError as err:
        raise ImportError(
            "Opening GRIB met for the particles needs cfgrib: "
            'pip install "runtraj[flexpart]"'
        ) from err
    datasets = [ds for path in files for ds in cfgrib.open_datasets(str(path))]
    if not datasets:
        raise ValueError("No GRIB files to open.")
    return from_flex_extract(datasets)


__all__ = [
    "catalog",
    "files_for",
    "grib_grid",
    "grib_valid_time",
    "holds_grib",
    "nxshift",
    "open_era5",
]

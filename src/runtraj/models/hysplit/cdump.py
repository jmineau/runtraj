"""
Read HYSPLIT's binary concentration file, ``cdump``, as a grid of :mod:`runtraj.concentration`.

``hycs_std`` writes one file per concentration grid (``conini.f`` writes
its header, ``condsk.f`` each sample): Fortran unformatted sequential
records, big-endian. :func:`read_cdump` reads one file; the model makes a
run's result from the files of its grids.
"""

from __future__ import annotations

import datetime as dt
import json
import struct
from pathlib import Path
from typing import BinaryIO

import numpy as np
import pandas as pd
import xarray as xr

from runtraj.concentration import CONCENTRATION_DIMS, describe_grid

#: The ``ichem`` values with which HYSPLIT counts one pollutant apart by
#: release location or by emission cycle, and the dimension each is read as.
SPLIT_ICHEM = {1: "source", 10: "release_time"}

#: What :func:`read_cdump` resolves a two-digit year with: years below this are 20YY, the rest 19YY.
_CENTURY_PIVOT = 40


class _Records:
    """
    The records of a Fortran unformatted sequential file, big-endian, as HYSPLIT writes them.

    Each record is its length in bytes (a 4-byte integer), the record, and
    its length again.
    """

    def __init__(self, handle: BinaryIO, path: Path) -> None:
        self._handle = handle
        self._path = path

    def next(self) -> bytes | None:
        """Return the next record, or ``None`` at the end of the file."""
        head = self._handle.read(4)
        if not head:
            return None
        if len(head) < 4:
            raise ValueError(f"{self._path} ends inside a record marker.")
        (size,) = struct.unpack(">i", head)
        body = self._handle.read(size)
        tail = self._handle.read(4)
        if len(body) < size or len(tail) < 4 or struct.unpack(">i", tail)[0] != size:
            raise ValueError(
                f"{self._path} is not a whole HYSPLIT concentration file: a "
                "record is cut off or its markers do not match."
            )
        return body

    def need(self, what: str) -> bytes:
        """Return the next record, which must exist."""
        record = self.next()
        if record is None:
            raise ValueError(f"{self._path} ends before its {what}.")
        return record


def _year(yy: int) -> int:
    """Return the year of HYSPLIT's two-digit *yy*: 1940 to 2039."""
    if yy >= 100:
        return yy
    return 2000 + yy if yy < _CENTURY_PIVOT else 1900 + yy


def _stamp(values: tuple[int, ...]) -> dt.datetime:
    """Return the time of a ``(yy, mm, dd, hh, mn)`` record."""
    yy, mm, dd, hh, mn = values[:5]
    return dt.datetime(_year(yy), mm, dd, hh, mn)


def read_cdump(
    path: str | Path, cmass: int = 0, ichem: int = 0, pollutant: str | None = None
) -> xr.Dataset:
    """
    Read one of HYSPLIT's binary concentration files (``cdump``) as a Dataset.

    The file holds one grid: its header (the met, the release locations, the
    grid, the levels, the pollutants), then, for each sampling time, one
    record per pollutant per level. With ``CPACK=1`` (HYSPLIT's default; 3
    is written the same way) a record lists only the cells that are not
    zero; with ``CPACK=0`` it holds every cell. Both read to the same
    Dataset.

    Parameters
    ----------
    path : str or Path
        The file ``hycs_std`` wrote.
    cmass : int, default 0
        The ``cmass`` setting the run had: 0 for concentrations, anything
        else for the mass in each cell. The file does not say, and the
        variables' ``units`` and ``long_name`` do.
    ichem : int, default 0
        The ``ichem`` setting the run had, which the file does not say
        either. With 6 the values are divided by air density, and the
        ``units`` say so. With 9 the first level reaches from the ground
        to a share of the mixed layer, and ``z`` says so. With 1 or 10
        what the file calls its pollutants are the release locations or
        the emission cycles of one pollutant, and the Dataset has one
        variable, named *pollutant*, with a dimension more (see Notes).
    pollutant : str, optional
        The name of the run's one pollutant, with *ichem* 1 or 10, which
        the file does not hold.

    Returns
    -------
    xarray.Dataset
        Dimensions ``time``, ``z``, ``lat``, ``lon``, and a float32
        variable per pollutant, as the module describes. ``lat`` and
        ``lon`` are the decimal degrees the grid was given, not the 4-byte
        floats the file holds (``40.266``, not ``40.26599884``). Its attributes
        hold ``cpack``, ``met_model`` (the met's four-letter name), and the
        release ``locations`` as ``[[lon, lat, height], ...]``. Two-digit
        years are read as 1940 to 2039.

    Notes
    -----
    With ``ichem=1`` HYSPLIT numbers the release locations in the place of
    the pollutants (``0001``, ``0002``, ...). They are read as the
    dimension ``source``, 1 to the number of locations, in the order of
    the release's points, with the coordinates ``source_lon``,
    ``source_lat`` and ``source_height`` from the file's header.

    With ``ichem=10`` HYSPLIT names each emission cycle by the tenths of
    an hour from the start of the run to the cycle's start. They are read
    as the dimension ``release_time``, the UTC time each cycle starts.

    With ``ichem=9`` each record of the first level holds the depth that
    level had for the last particle counted, in place of its height, so
    that depth is not checked, and not kept.

    Raises
    ------
    ValueError
        If the file is cut off, is not a concentration file, or was written
        with ``CPACK=2`` (one point per grid), which this does not read; or
        if its pollutants are not what *ichem* 1 or 10 writes.

    Examples
    --------
    >>> ds = read_cdump("cdump")  # doctest: +SKIP
    >>> ds["TEST"].sel(z=100).max()  # doctest: +SKIP
    """
    path = Path(path)
    with path.open("rb") as handle:
        records = _Records(handle, path)
        try:
            ds = _read_records(records, path, ichem)
        except struct.error as error:
            raise ValueError(
                f"{path} is not a HYSPLIT concentration file: {error}."
            ) from None
    if ichem in SPLIT_ICHEM:
        if pollutant is None:
            raise ValueError(
                f"read_cdump needs the pollutant's name with ichem={ichem}."
            )
        ds = _split(ds, path, ichem, pollutant)
    units, long_name = _meaning(cmass, ichem)
    describe_grid(ds, units=units, long_name=long_name)
    if ichem == 9:
        ds["z"].attrs["long_name"] = (
            "top of the level above ground, except the first, which is the "
            "percentage of the mixed layer depth its level reaches from "
            "the ground"
        )
    return ds


def _meaning(cmass: int, ichem: int) -> tuple[str, str]:
    """Return the ``units`` and ``long_name`` of a grid's variables in a run with *cmass* and *ichem*."""
    unit = "(mass in the unit of the emission rate)"
    if cmass:
        # MASSUM, which hycs_std calls instead of CONSUM, adds each particle's
        # mass to its cell: not per volume, and nothing at z = 0, where CONSUM
        # counts deposition.
        if ichem == 6:
            return (
                f"mass m3 kg-1 {unit}",
                "{name} mass in the cell, divided by air density; no deposition at z = 0",
            )
        return f"mass {unit}", "{name} mass in the cell; no deposition at z = 0"
    if ichem == 6:
        return (
            f"mass kg-1, mass m-2 at z = 0 {unit}",
            "{name} mass per mass of air (concentration divided by air "
            "density), or deposition at z = 0",
        )
    return (
        f"mass m-3, mass m-2 at z = 0 {unit}",
        "{name} concentration, or deposition at z = 0",
    )


def _split(ds: xr.Dataset, path: Path, ichem: int, pollutant: str) -> xr.Dataset:
    """
    Return *ds* with its variables, the sources or cycles of one pollutant, as one variable with a dimension more.

    *ds* is the file as written with ``ichem`` 1 or 10: a variable per
    release location, named in hexadecimal from ``0001``, or per emission
    cycle, named by the tenths of an hour to its start.
    """
    names = [str(name) for name in ds.data_vars]
    dim = SPLIT_ICHEM[ichem]
    try:
        numbers = [int(name, 16 if ichem == 1 else 10) for name in names]
    except ValueError:
        numbers = []
    locations = json.loads(ds.attrs["locations"])
    if ichem == 1:
        ok = numbers == list(range(1, len(locations) + 1))
        coords = {
            "source": ("source", np.asarray(numbers, dtype=np.int32)),
            "source_lon": ("source", [loc[0] for loc in locations]),
            "source_lat": ("source", [loc[1] for loc in locations]),
            "source_height": ("source", [loc[2] for loc in locations]),
        }
    else:
        ok = bool(numbers) and numbers == sorted(set(numbers)) and numbers[0] == 0
        start = pd.Timestamp(ds.attrs["start"])
        times = [start + pd.Timedelta(minutes=6 * tenths) for tenths in numbers]
        coords = {
            "release_time": (
                "release_time",
                pd.DatetimeIndex(times, dtype="datetime64[ns]"),
            )
        }
    if not ok:
        raise ValueError(
            f"{path} is not the file of a run with ichem={ichem}: its "
            f"pollutants are {names}."
        )
    data = np.stack([ds[name].to_numpy() for name in names])
    out = ds.drop_vars(names).assign_coords(coords)
    out[pollutant] = ((dim, *CONCENTRATION_DIMS), data)
    if ichem == 1:
        out["source"].attrs = {
            "long_name": "release location, numbered from 1 in the order of the release's points"
        }
        out["source_lon"].attrs = {"units": "degrees_east"}
        out["source_lat"].attrs = {"units": "degrees_north"}
        out["source_height"].attrs = {"units": "m"}
    else:
        out["release_time"].attrs = {"long_name": "start of the emission cycle"}
    return out


def _read_records(records: _Records, path: Path, ichem: int) -> xr.Dataset:
    """Read the records of a ``cdump`` file, as :func:`read_cdump` does."""
    head = records.need("header")
    met_model = head[:4].decode("ascii", errors="replace").strip()
    *_start, nloc, cpack = struct.unpack(">7i", head[4:32])
    if cpack == 2:
        raise ValueError(
            f"{path} was written with CPACK=2, one point per grid, which "
            "runtraj does not read. Use cpack 0 or 1."
        )
    locations = []
    start = None
    for _ in range(nloc):
        yy, mm, dd, hh, lat, lon, height, mn = struct.unpack(
            ">4i3fi", records.need("release locations")
        )
        locations.append([float(lon), float(lat), float(height)])
        start = start or _stamp((yy, mm, dd, hh, mn))
    nlat, nlon, dlat, dlon, lat1, lon1 = struct.unpack(">2i4f", records.need("grid"))
    level_record = records.need("levels")
    (nz,) = struct.unpack(">i", level_record[:4])
    levels = struct.unpack(f">{nz}i", level_record[4 : 4 + 4 * nz])
    type_record = records.need("pollutants")
    (ntyp,) = struct.unpack(">i", type_record[:4])
    names = [
        type_record[4 + 4 * k : 8 + 4 * k].decode("ascii", errors="replace").strip()
        for k in range(ntyp)
    ]
    if len(set(names)) != len(names):
        raise ValueError(f"{path} names a pollutant twice: {names}.")

    starts: list[dt.datetime] = []
    ends: list[dt.datetime] = []
    samples: list[np.ndarray] = []
    while (record := records.next()) is not None:
        starts.append(_stamp(struct.unpack(">6i", record[:24])))
        ends.append(_stamp(struct.unpack(">6i", records.need("sample end")[:24])))
        grid = np.zeros((ntyp, nz, nlat, nlon), dtype=np.float32)
        # One record per pollutant per level, the levels of a pollutant together.
        for kt in range(ntyp):
            for kl in range(nz):
                # With ICHEM=9 a record of the first level holds a depth, not its height.
                height = None if ichem == 9 and kl == 0 else int(levels[kl])
                _read_level(
                    records.need("concentrations"),
                    grid[kt, kl],
                    names[kt],
                    height,
                    cpack,
                    path,
                )
        samples.append(grid)

    data = (
        np.stack(samples, axis=1)
        if samples
        else np.zeros((ntyp, 0, nz, nlat, nlon), dtype=np.float32)
    )
    coords = {
        "time": ("time", pd.DatetimeIndex(starts, dtype="datetime64[ns]")),
        "time_end": ("time", pd.DatetimeIndex(ends, dtype="datetime64[ns]")),
        "z": ("z", np.asarray(levels, dtype=np.int32)),
        "lat": ("lat", _cell_centers(lat1, dlat, nlat)),
        "lon": ("lon", _cell_centers(lon1, dlon, nlon)),
    }
    variables = {name: (CONCENTRATION_DIMS, data[k]) for k, name in enumerate(names)}
    ds = xr.Dataset(variables, coords=coords)
    ds.attrs = {
        "cpack": int(cpack),
        "met_model": met_model,
        "locations": json.dumps(locations),
        "start": start.isoformat() if start else "",
    }
    return ds


def _cell_centers(first: float, step: float, count: int) -> np.ndarray:
    """
    Return the centers of *count* cells, as the decimal degrees HYSPLIT was given.

    ``cdump`` holds the first center and the spacing as 4-byte floats, so
    that ``40.266`` reads as ``40.26599884``. The centers are rounded to
    the decimals a 4-byte float holds at the largest of them: a step in the
    last decimal is at least twice the spacing of floats there. That is 4
    decimals from 64 to 360 degrees, where a spacing of 0.001 degree is
    still kept whole, 5 from 8 to 64, where 40.266 reads back as 40.266, and
    6 below 8.
    """
    centers = first + step * np.arange(count, dtype=np.float64)
    largest = float(np.abs(centers).max())
    if largest == 0:
        return centers
    decimals = int(np.floor(-np.log10(2 * float(np.spacing(np.float32(largest))))))
    return np.round(centers, decimals)


def _read_level(
    record: bytes,
    cells: np.ndarray,
    name: str,
    height: int | None,
    cpack: int,
    path: Path,
) -> None:
    """
    Put a ``cdump`` record, one pollutant's one level of a sample, into *cells*, ``(lat, lon)``.

    The record names its pollutant and its level's height, which must be
    *name* and *height* (no height is checked when *height* is ``None``).
    """
    found = record[:4].decode("ascii", errors="replace").strip()
    (level,) = struct.unpack(">i", record[4:8])
    if found != name or (height is not None and level != height):
        raise ValueError(
            f"{path} holds {found!r} at {level} m where {name!r} at "
            f"{height} m is expected: it is not the file of a run with "
            "these settings."
        )
    nlat, nlon = cells.shape
    if cpack == 0:
        values = np.frombuffer(record, dtype=">f4", count=nlat * nlon, offset=8)
        # Fortran order: longitude varies fastest.
        cells[:] = values.reshape(nlat, nlon)
        return
    (count,) = struct.unpack(">i", record[8:12])
    if count == 0:
        return
    packed = np.frombuffer(
        record,
        dtype=np.dtype([("i", ">i2"), ("j", ">i2"), ("c", ">f4")]),
        count=count,
        offset=12,
    )
    # HYSPLIT counts cells from 1.
    cells[packed["j"].astype(int) - 1, packed["i"].astype(int) - 1] = packed["c"]


__all__ = ["read_cdump"]

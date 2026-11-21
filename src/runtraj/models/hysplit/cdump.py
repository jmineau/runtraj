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


def read_cdump(path: str | Path) -> xr.Dataset:
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

    Returns
    -------
    xarray.Dataset
        Dimensions ``time``, ``z``, ``lat``, ``lon``, and a float32
        variable per pollutant, as the module describes. Its attributes
        hold ``cpack``, ``met_model`` (the met's four-letter name), and the
        release ``locations`` as ``[[lon, lat, height], ...]``. Two-digit
        years are read as 1940 to 2039.

    Raises
    ------
    ValueError
        If the file is cut off, is not a concentration file, or was written
        with ``CPACK=2`` (one point per grid), which this does not read.

    Examples
    --------
    >>> ds = read_cdump("cdump")  # doctest: +SKIP
    >>> ds["TEST"].sel(z=100).max()  # doctest: +SKIP
    """
    path = Path(path)
    with path.open("rb") as handle:
        records = _Records(handle, path)
        try:
            return _read_records(records, path)
        except struct.error as error:
            raise ValueError(
                f"{path} is not a HYSPLIT concentration file: {error}."
            ) from None


def _read_records(records: _Records, path: Path) -> xr.Dataset:
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
    for _ in range(nloc):
        _y, _m, _d, _h, lat, lon, height, _mn = struct.unpack(
            ">4i3fi", records.need("release locations")
        )
        locations.append([float(lon), float(lat), float(height)])
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
    level_index = {int(z): k for k, z in enumerate(levels)}

    starts: list[dt.datetime] = []
    ends: list[dt.datetime] = []
    samples: list[np.ndarray] = []
    while (record := records.next()) is not None:
        starts.append(_stamp(struct.unpack(">6i", record[:24])))
        ends.append(_stamp(struct.unpack(">6i", records.need("sample end")[:24])))
        grid = np.zeros((ntyp, nz, nlat, nlon), dtype=np.float32)
        for _ in range(ntyp * nz):
            _read_level(records.need("concentrations"), grid, names, level_index, cpack)
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
        "lat": ("lat", (lat1 + dlat * np.arange(nlat)).astype(np.float64)),
        "lon": ("lon", (lon1 + dlon * np.arange(nlon)).astype(np.float64)),
    }
    variables = {name: (CONCENTRATION_DIMS, data[k]) for k, name in enumerate(names)}
    ds = xr.Dataset(variables, coords=coords)
    ds.attrs = {
        "cpack": int(cpack),
        "met_model": met_model,
        "locations": json.dumps(locations),
    }
    return describe_grid(ds)


def _read_level(
    record: bytes,
    grid: np.ndarray,
    names: list[str],
    level_index: dict[int, int],
    cpack: int,
) -> None:
    """Put one pollutant's one level of a sample, a ``cdump`` record, into *grid*."""
    name = record[:4].decode("ascii", errors="replace").strip()
    (height,) = struct.unpack(">i", record[4:8])
    kt, kl = names.index(name), level_index[height]
    nlat, nlon = grid.shape[2:]
    if cpack == 0:
        values = np.frombuffer(record, dtype=">f4", count=nlat * nlon, offset=8)
        # Fortran order: longitude varies fastest.
        grid[kt, kl] = values.reshape(nlat, nlon)
        return
    (count,) = struct.unpack(">i", record[8:12])
    if count == 0:
        return
    cells = np.frombuffer(
        record,
        dtype=np.dtype([("i", ">i2"), ("j", ">i2"), ("c", ">f4")]),
        count=count,
        offset=12,
    )
    # HYSPLIT counts cells from 1.
    grid[kt, kl, cells["j"].astype(int) - 1, cells["i"].astype(int) - 1] = cells["c"]


__all__ = ["read_cdump"]

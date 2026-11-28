"""
Read HYSPLIT's trajectory file, ``tdump``, as a trajectory table of :mod:`runtraj.trajectories`.

``hyts_std`` writes one text file per run (``trjset.f`` writes its header,
``trjdsk.f`` each row): the met files, the trajectories and where each
started, the names of the values written along them, then one fixed-width
row per trajectory per output time.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .config import TRAJECTORY_DIAGNOSTICS
from .control import _year

#: The trajectory table's column for each of HYSPLIT's labels in the file.
#: A label not listed here keeps HYSPLIT's name, in lowercase.
TDUMP_COLUMNS = {
    "PRESSURE": "pres",
    **{label: column for _, label, column in TRAJECTORY_DIAGNOSTICS},
}

#: The format of the file runtraj reads, the second number of its first line.
_FORMAT = 1

#: The widths of a row's fields before the height (``trjdsk.f``:
#: ``8I6,F8.1,2F9.3``): trajectory, met grid, year, month, day, hour,
#: minute, forecast hour, age in hours, latitude, longitude. The height and
#: each value after it are 9 wide (``1X,F8.1``).
_ROW_WIDTHS = (6, 6, 6, 6, 6, 6, 6, 6, 8, 9, 9)
_VALUE_WIDTH = 9


def read_tdump(path: str | Path) -> pd.DataFrame:
    """
    Read a trajectory file ``hyts_std`` wrote.

    Parameters
    ----------
    path : str or Path
        The file, ``tdump`` in a run's workdir.

    Returns
    -------
    pandas.DataFrame
        One row per trajectory per output time, in the file's order, with
        ``trajectory``, ``time`` (UTC), ``age`` (seconds since the
        trajectory's first row, which is where it started), ``lon``,
        ``lat``, ``zagl``, ``pres`` (hPa), and a column for each other
        value the run wrote (:data:`TDUMP_COLUMNS`). Its ``attrs`` hold
        the file's header: ``trajectories``, how many the run followed,
        ``direction`` (``FORWARD`` or ``BACKWARD``) and
        ``vertical_motion``, HYSPLIT's name for the method.

    Raises
    ------
    ValueError
        If the file is not a trajectory file in the format runtraj reads,
        or ends inside its header.

    Notes
    -----
    The rows are read by the widths Fortran wrote them with, not by
    spaces, so a value that fills its field is still read. ``age`` comes from each row's date and time, not from the file's
    column of hours, which has one decimal. The height is above ground only
    when the run had ``KAGL=1``, as runtraj's do: the file does not say.
    The file's columns for the met grid and the forecast hour are left out.
    """
    path = Path(path)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    try:
        n_met, version = (int(v) for v in lines[0].split()[:2])
    except (IndexError, ValueError) as error:
        raise ValueError(f"{path.name} is not a HYSPLIT trajectory file.") from error
    if version != _FORMAT:
        raise ValueError(
            f"{path.name} is in trajectory file format {version}; runtraj "
            f"reads format {_FORMAT}."
        )
    try:
        at = 1 + n_met
        count, direction, motion = lines[at].split()[:3]
        at += 1 + int(count)
        labels = lines[at].split()[1 : 1 + int(lines[at].split()[0])]
    except (IndexError, ValueError) as error:
        raise ValueError(f"{path.name} is not a HYSPLIT trajectory file.") from error
    names = [TDUMP_COLUMNS.get(label, label.lower()) for label in labels]
    widths = [*_ROW_WIDTHS, *([_VALUE_WIDTH] * (1 + len(names)))]
    stops = np.cumsum(widths)
    rows = [
        [line[stop - width : stop] for width, stop in zip(widths, stops, strict=True)]
        for line in lines[at + 1 :]
        if line.strip()
    ]
    raw = pd.DataFrame(rows, columns=range(len(widths))).apply(
        pd.to_numeric, errors="coerce"
    )
    time = pd.to_datetime(
        {
            "year": raw[2].map(_year),
            "month": raw[3],
            "day": raw[4],
            "hour": raw[5],
            "minute": raw[6],
        },
        utc=True,
    )
    table = pd.DataFrame({"trajectory": raw[0].astype("int32"), "time": time})
    start = table.groupby("trajectory")["time"].transform("first")
    table["age"] = (table["time"] - start).dt.total_seconds().astype("int32")
    table["lon"] = raw[10]
    table["lat"] = raw[9]
    table["zagl"] = raw[11]
    for k, name in enumerate(names):
        table[name] = raw[12 + k]
    table.attrs = {
        "trajectories": int(count),
        "direction": direction,
        "vertical_motion": motion,
    }
    return table


__all__ = ["TDUMP_COLUMNS", "read_tdump"]

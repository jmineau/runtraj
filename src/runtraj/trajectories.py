"""
Trajectories: what a transport model makes when it follows single air parcels, with no turbulence.

A run in a trajectory mode, such as HYSPLIT's ``mode: trajectory``, gives
one table per release: a row for each trajectory at each output time. A
point release starts one trajectory, and a multipoint release one from
each of its points, numbered in the order of the points::

    trajectories = sim.trajectories
    trajectories[trajectories["trajectory"] == 1]

The table has the columns of :data:`TRAJECTORY_SCHEMA`:

- ``trajectory``, the trajectory's number, from 1;
- ``time``, when the row is (UTC), and ``age``, the seconds since the
  release, negative in a backward run. Each trajectory has a row at
  ``age = 0``, where it started;
- ``lon`` and ``lat``, in degrees, and ``zagl``, the height above ground
  in meters;

and one column for each value the model wrote along the trajectory, such
as HYSPLIT's ``pres``. It is not the particle table
(:mod:`runtraj.particles`): there is no ``foot`` and no ``weight``, and the
particle transforms and stages do not read it.

A transport model reads its own output into such a table (HYSPLIT's
:func:`runtraj.models.hysplit.read_tdump`). :func:`write_trajectories` and
:func:`read_trajectories` store and read it, as one Parquet file per
release.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from runtraj._paths import location, readable
from runtraj.particles.table import OLD_TEMP, write_release_file

if TYPE_CHECKING:
    from upath import UPath

    from runtraj.releases import Release

#: The extension of a stored trajectory table.
TRAJECTORIES_SUFFIX = ".parquet"

#: The columns every trajectory table has, with what each holds. A model
#: adds a column for each value it writes along the trajectory.
TRAJECTORY_SCHEMA: dict[str, str] = {
    "trajectory": "The trajectory's number in its release, from 1.",
    "time": "Time of the row, in UTC.",
    "age": "Seconds since the release, negative in a backward run.",
    "lon": "Longitude, in degrees east.",
    "lat": "Latitude, in degrees north.",
    "zagl": "Height above ground, in meters.",
}

#: The columns stored as int32.
_INT_COLUMNS = ("trajectory", "age")


def check_trajectories(result: Any) -> None:
    """
    Raise unless *result* is a trajectory table: a DataFrame with the columns of :data:`TRAJECTORY_SCHEMA` and at least one row.

    Raises
    ------
    ValueError
        Saying what is wrong.

    Examples
    --------
    >>> check_trajectories(pd.DataFrame({"trajectory": [1]}))
    Traceback (most recent call last):
    ...
    ValueError: A trajectory table needs the columns ['time', 'age', 'lon', 'lat', 'zagl'].
    """
    if not isinstance(result, pd.DataFrame):
        raise ValueError(
            f"A trajectories result is a pandas.DataFrame, not {type(result).__name__}."
        )
    missing = [name for name in TRAJECTORY_SCHEMA if name not in result.columns]
    if missing:
        raise ValueError(f"A trajectory table needs the columns {missing}.")
    if result.empty:
        raise ValueError("The transport model wrote no trajectory.")


def write_trajectories(
    path: str | Path | UPath,
    result: pd.DataFrame,
    release: Release,
    settings: Mapping[str, Any],
    met_files: list[Path],
    metadata: Mapping[bytes, bytes] | None = None,
) -> Path | UPath:
    """
    Write a trajectory table to a Parquet file that :func:`read_trajectories` reads alone.

    ``trajectory`` and ``age`` are stored as int32. A ``release`` column
    holds the release id, so a scan of many files can tell releases apart.
    The release, the run's settings and the met files go in the file's
    metadata, with *metadata*, such as the settings hash.

    Parameters
    ----------
    path : str, Path or UPath
        File to write.
    result : pandas.DataFrame
        The trajectory table (:func:`check_trajectories`).
    release : Release
        The release the trajectories started from.
    settings : mapping
        The run's settings, as its folder records them.
    met_files : list of Path
        The met files the run read.
    metadata : mapping, optional
        More file metadata, as ``{b"runtraj:...": b"..."}``.

    Returns
    -------
    Path or UPath
        The path written to.
    """
    check_trajectories(result)
    data = result.drop(columns=["release"], errors="ignore")
    data = data.assign(
        **{name: data[name].to_numpy().astype(np.int32) for name in _INT_COLUMNS}
    )
    return write_release_file(path, data, release, settings, met_files, metadata)


def read_trajectories(path: str | Path | UPath) -> pd.DataFrame:
    """
    Read a trajectory file :func:`write_trajectories` wrote.

    The ``release`` column, which the file holds for scans of many files,
    is left out: one file is one release.

    Raises
    ------
    FileNotFoundError
        If there is no file at *path*.

    Examples
    --------
    >>> trajectories = read_trajectories(sim.result_path)  # doctest: +SKIP
    """
    where = location(path)
    if not where.exists():
        raise FileNotFoundError(f"No trajectory file at {where}.")
    with readable(where) as source:
        pf = pq.ParquetFile(source)
        columns = [c for c in pf.schema_arrow.names if c != "release"]
        if "temp" in columns:
            raise ValueError(OLD_TEMP.format(what=f"The file {where}"))
        return pf.read(columns=columns).to_pandas()


def trajectories_from_table(table: pa.Table) -> pd.DataFrame:
    """
    Return a table of many releases' trajectories as one DataFrame.

    The table is what :meth:`runtraj.output.Output.table` reads: a
    ``release`` column, the stored columns, and ``date``, which is dropped.
    ``release`` comes back as strings.
    """
    if "temp" in table.column_names:
        raise ValueError(OLD_TEMP.format(what="A trajectory file in the table"))
    data = table.unify_dictionaries().to_pandas()
    data = data.drop(columns=["date"], errors="ignore")
    if not data.empty:
        data["release"] = data["release"].astype(str)
    return data


__all__ = [
    "TRAJECTORIES_SUFFIX",
    "TRAJECTORY_SCHEMA",
    "check_trajectories",
    "read_trajectories",
    "trajectories_from_table",
    "write_trajectories",
]

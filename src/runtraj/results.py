"""
The types of result a transport model makes, and how each is stored.

A transport model makes one type of result per run, and every model that
makes a type follows its one schema:

- ``particles``: the particle table (:mod:`runtraj.particles`), one
  Parquet file per release.
- ``concentration``: grids of concentration
  (:mod:`runtraj.concentration`), one NetCDF file per release.
- ``trajectories``: the trajectory table (:mod:`runtraj.trajectories`),
  one Parquet file per release.

A model says which type a config makes with an optional
``result(config)`` (:class:`runtraj.models.TransportModel`), and makes
particles when it does not say. The type names the tree of the output
directory its files are kept in (``particles/``, ``concentration/``, ``trajectories/``),
beside the trees of the stages. A stage reads particles, so a variant that
makes another type has no stages.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from runtraj.concentration import (
    CONCENTRATION_SUFFIX,
    check_concentration,
    read_concentration,
    write_concentration,
)
from runtraj.particles import check_particles, read_particles, write_particles
from runtraj.trajectories import (
    TRAJECTORIES_SUFFIX,
    check_trajectories,
    read_trajectories,
    write_trajectories,
)

if TYPE_CHECKING:
    from upath import UPath

    from runtraj.releases import Release


def _check_particle_table(result: Any) -> None:
    """Raise unless *result* is a particle table with rows."""
    if not isinstance(result, pd.DataFrame):
        raise ValueError(
            f"A particles result is a pandas.DataFrame, not {type(result).__name__}."
        )
    if result.empty:
        raise ValueError("The transport model wrote no particles.")
    check_particles(result)


@dataclass(frozen=True)
class ResultType:
    """
    One type of result: its name, its file's suffix, and how a file of it is checked, written, and read.

    Attributes
    ----------
    name : str
        The type's name, and the name of its tree in the output directory.
    suffix : str
        The extension of a release's file.
    check : callable
        ``check(result)``: raises ``ValueError`` unless *result* is one of
        this type, with something in it.
    write : callable
        ``write(path, result, release, settings, met_files, metadata)``:
        writes a release's file, which reads alone.
    read : callable
        ``read(path)``: returns the result a file holds.
    """

    name: str
    suffix: str
    check: Callable[[Any], None]
    write: Callable[..., Path | UPath]
    read: Callable[[Any], Any]


#: The result types, by name.
RESULT_TYPES: dict[str, ResultType] = {
    "particles": ResultType(
        "particles", ".parquet", _check_particle_table, write_particles, read_particles
    ),
    "concentration": ResultType(
        "concentration",
        CONCENTRATION_SUFFIX,
        check_concentration,
        write_concentration,
        read_concentration,
    ),
    "trajectories": ResultType(
        "trajectories",
        TRAJECTORIES_SUFFIX,
        check_trajectories,
        write_trajectories,
        read_trajectories,
    ),
}


def result_type(name: str) -> ResultType:
    """
    Return the result type called *name*.

    Raises
    ------
    ValueError
        If no type has that name.
    """
    found = RESULT_TYPES.get(name)
    if found is None:
        raise ValueError(
            f"No result type {name!r}: a transport model makes one of "
            f"{sorted(RESULT_TYPES)}."
        )
    return found


def model_result(model: Any, config: Any) -> str:
    """
    Return the type of result *model* makes with *config*, its ``result(config)``, checked.

    Raises
    ------
    ValueError
        If the model names a type that does not exist.
    """
    return result_type(model.result(config)).name


def write_result(
    name: str,
    path: str | Path | UPath,
    result: Any,
    release: Release,
    settings: Mapping[str, Any],
    met_files: list[Path],
    metadata: dict[bytes, bytes] | None = None,
) -> Path | UPath:
    """Write *result*, of the type called *name*, as that type writes a release's file."""
    return result_type(name).write(
        path, result, release, settings, met_files, metadata=metadata
    )


__all__ = [
    "RESULT_TYPES",
    "ResultType",
    "model_result",
    "result_type",
    "write_result",
]

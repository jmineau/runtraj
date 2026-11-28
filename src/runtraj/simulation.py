"""
A simulation, one release run under one variant, and where its results are.

The workers and the stages use it. A package that adds a stage can subclass
:class:`Simulation` with the reads of what its stage makes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from runtraj.output import Output, completed
from runtraj.releases import Release
from runtraj.results import result_type

if TYPE_CHECKING:
    import xarray as xr
    from upath import UPath

    from runtraj.config import Variant

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Simulation:
    """
    One release run under one variant, as one realization of an ensemble or alone.

    A simulation is a value: its release, its variant, and the output
    directory its results are in. From those it knows where the transport
    model's result (its particles, or another type: :attr:`result_path`),
    the file of each stage (:meth:`path`), and its log are, whether they
    exist (:meth:`has`, :attr:`is_complete`), and how to read them. It runs
    nothing itself; the workers in :mod:`runtraj.execution` run the transport
    model and the stages.

    You rarely build one yourself. Get it from a project instead, as in
    ``project.simulation(release_id, "hrrr")``.

    Parameters
    ----------
    release : Release
        Where and when particles are released.
    variant : Variant
        The variant, resolved: its configs, and the hashes that find its
        particles folder and its stages' folders.
    output : Output
        The output directory.
    directory : Path, optional
        The project directory, where a relative file name in the settings
        starts. ``None`` starts it from
        the working directory.
    realization : int, optional
        Which realization of an ensemble variant (``realizations: N``),
        ``0`` to ``N - 1``. ``None`` for a variant that runs once.

    Raises
    ------
    ValueError
        If *realization* is not one the variant runs as.
    """

    release: Release
    variant: Variant
    output: Output
    directory: Path | None = None
    realization: int | None = None

    def __post_init__(self) -> None:
        self.variant.check_realization(self.realization)

    def __repr__(self) -> str:
        realization = (
            "" if self.realization is None else f", realization={self.realization}"
        )
        return (
            f"Simulation(release={self.release.id!r}, "
            f"variant={self.variant.name!r}{realization})"
        )

    def __str__(self) -> str:
        name = f"{self.release.id}/{self.variant.name}"
        return name if self.realization is None else f"{name}/{self.realization}"

    def __eq__(self, other: object) -> bool:
        # One simulation whatever its class: the workers build this class and
        # runtraj.Project the subclass, which adds only reads.
        if not isinstance(other, Simulation):
            return NotImplemented
        mine = tuple(getattr(self, f.name) for f in fields(self))
        return mine == tuple(getattr(other, f.name) for f in fields(other))

    def __hash__(self) -> int:
        return hash((self.id, self.output))

    # -- identity ----------------------------------------------------------

    @property
    def id(self) -> tuple[str, str, int | None]:
        """``(release.id, variant.name, realization)``, the row of ``project.simulations`` it is."""
        return (self.release.id, self.variant.name, self.realization)

    @property
    def transport(self) -> Any:
        """The transport config this simulation runs with. It is the variant's, with ``seed + realization`` for an ensemble."""
        return self.variant.transport_for(self.realization)

    # -- where the results are ---------------------------------------------

    def path(self, kind: str) -> Path | UPath | None:
        """
        Return the path of this simulation's file of *kind* in the output directory.

        *kind* is a result type, ``"particles"`` for most variants
        (:attr:`runtraj.config.Variant.result`), or the name of a stage,
        such as ``"footprint"``. ``None`` before the variant's folder of
        that kind exists, for a stage the variant does not use, and for a
        result type it does not make. The file may not
        exist yet (:meth:`has`). A stage that reads another stage's file
        finds it here, and reads it with that stage's own reader.

        Examples
        --------
        >>> sim.path("footprint")  # doctest: +SKIP
        """
        return self.output.path(kind, self.variant, self.release.id, self.realization)

    def has(self, kind: str) -> bool:
        """
        Return whether this simulation's file of *kind* exists.

        *kind* is a result type, such as ``"particles"``, or the name of a
        stage. A stage's file is a finished result whatever it holds.
        """
        path = self.path(kind)
        return path is not None and path.exists()

    @property
    def particles_path(self) -> Path | UPath | None:
        """Path of the particle file in the output directory, or ``None`` before its folder exists."""
        return self.path("particles")

    @property
    def result_path(self) -> Path | UPath | None:
        """
        Path of the transport model's result in the output directory, or ``None`` before its folder exists.

        The particle file for a variant that makes particles
        (:attr:`particles_path`), the concentration file for one that makes
        concentration: the type of :attr:`runtraj.config.Variant.result`.
        """
        return self.path(self.variant.result)

    @property
    def log_path(self) -> Path | UPath | None:
        """Path of the transport model's log in the output directory, or ``None`` before its folder exists."""
        return self.output.log_path(self.variant, self.release.id, self.realization)

    @property
    def kept_workdir(self) -> Path | UPath | None:
        """
        Where a failed run's working directory is kept, or ``None`` before its folder exists.

        The directory, under ``scratch/`` in the output directory, holds
        the transport model's files as the run left them: for HYSPLIT,
        CONTROL, SETUP.CFG, and its own output. It exists only after a
        failed run, or after any run with ``keep_workdir`` set.
        """
        return self.output.kept_workdir(self.variant, self.release.id, self.realization)

    # -- presence and completion -------------------------------------------

    @property
    def has_particles(self) -> bool:
        """Whether the particle file exists."""
        return self.has("particles")

    @property
    def has_result(self) -> bool:
        """
        Whether the transport model's result exists, whatever its type.

        :attr:`has_particles`, for a variant that makes particles.
        """
        return self.has(self.variant.result)

    @property
    def is_complete(self) -> bool:
        """
        Whether every expected result exists.

        That is the transport model's result (the particles, for most
        variants), and the file of every stage the variant uses. It is :func:`runtraj.output.completed`,
        the one definition of done, for one release, from a file check for
        each.
        """
        rid = self.release.id
        mine: frozenset[str] = frozenset([rid])
        none: frozenset[str] = frozenset()
        kinds = (self.variant.result, *self.variant.stage_order)
        return rid in completed(*(mine if self.has(k) else none for k in kinds))

    # -- status ------------------------------------------------------------

    @property
    def failure(self) -> dict[str, Any] | None:
        """
        Why this simulation is not complete, as the worker recorded it, or ``None``.

        ``None`` when it is complete, or has not run, or what failed has
        since succeeded. Otherwise a dict with ``stage`` (``"particles"``,
        or the variant's other result type, for the transport model run, or the name of the stage that failed,
        such as ``"footprint"``), ``reason`` (a short cause such as
        ``"MET_COVERAGE"``, or the error's class when it has none),
        ``message``, and ``time``. An unexpected error also has a
        ``traceback``. A failed transport model run fails every variant
        that shares its particles. Of several stages that failed, the
        first in the order they run is given. :attr:`log` reads the model's log and
        :attr:`kept_workdir` is where its workdir was kept.

        Examples
        --------
        >>> sim.failure  # doctest: +SKIP
        {'stage': 'particles', 'reason': 'MET_COVERAGE',
         'message': 'HYSPLIT: start point not within (x,y,t) any data file', ...}
        """
        variant, rid = self.variant, self.release.id
        made = variant.result
        if not self.has(made):
            return self.output.failure(made, variant, rid, self.realization)
        # A stage can have failed only where what it reads exists: the first
        # such stage with a record, in the order the stages run.
        there = {made}
        for name in variant.stage_order:
            if self.has(name):
                there.add(name)
            elif there.issuperset(variant.stage(name).reads):
                record = self.output.failure(name, variant, rid, self.realization)
                if record is not None:
                    return record
        return None

    # -- reading the results -----------------------------------------------

    @property
    def log(self) -> str:
        """
        Text of the transport model's log.

        Raises
        ------
        FileNotFoundError
            If the log has not been written yet, or the transport model
            wrote none.
        """
        log_path = self.log_path
        if log_path is None or not log_path.exists():
            raise FileNotFoundError(f"No log for {self} yet.")
        return log_path.read_text(encoding="utf-8")

    @cached_property
    def particles(self) -> pd.DataFrame:
        """
        The particle table, one row per particle per output step.

        The columns are the variables the transport model writes (for
        HYSPLIT, those its ``varsiwant`` names), under the particle table's
        names (``particle``, ``age`` in seconds since release,
        ``lon``, ``lat``, ``zagl``, ``foot``, ...), plus ``time`` (UTC)
        and ``release_height`` (for column and multipoint releases). Read from the output directory on first
        access and kept. Check
        :attr:`has_particles` first when the run may not have finished.

        Raises
        ------
        FileNotFoundError
            If the particles have not been written yet. Nothing is kept, so
            a read after the run finishes loads them.
        """
        return self._read("particles")

    @cached_property
    def result(self) -> Any:
        """
        The transport model's result, of the type the variant makes.

        :attr:`particles` for a variant that makes particles,
        :attr:`concentration` for one that makes concentration
        (:attr:`runtraj.config.Variant.result`). It is that attribute, so
        the file is read once whichever name reads it.

        Raises
        ------
        FileNotFoundError
            If the result has not been written yet.

        Examples
        --------
        >>> sim.result  # doctest: +SKIP
        """
        return getattr(self, self.variant.result)

    def _read(self, kind: str) -> Any:
        """Return the result of type *kind*, read by its type's reader."""
        path = self.path(kind)
        if path is None or not path.exists():
            made = self.variant.result
            if made != kind:
                raise FileNotFoundError(f"{self} makes {made}, not {kind}.")
            raise FileNotFoundError(f"{self} has no {kind} yet.")
        return result_type(kind).read(path)

    @cached_property
    def concentration(self) -> xr.DataTree:
        """
        The concentration result, a child per grid (:mod:`runtraj.concentration`).

        For a variant that makes concentration, such as HYSPLIT's ``mode:
        concentration``. Read from the output directory on first access and
        kept. Check :attr:`has_result` first when the run may not have
        finished.

        Raises
        ------
        FileNotFoundError
            If the result has not been written yet, or the variant makes
            another type of result.

        Examples
        --------
        >>> sim.concentration["local"]["TEST"].sel(z=100).max()  # doctest: +SKIP
        """
        return self._read("concentration")

    @cached_property
    def trajectories(self) -> pd.DataFrame:
        """
        The trajectory table, one row per trajectory per output time (:mod:`runtraj.trajectories`).

        For a variant that makes trajectories, such as HYSPLIT's ``mode:
        trajectory``. Read from the output directory on first access and
        kept. Check :attr:`has_result` first when the run may not have
        finished.

        Raises
        ------
        FileNotFoundError
            If the result has not been written yet, or the variant makes
            another type of result.

        Examples
        --------
        >>> sim.trajectories.groupby("trajectory")["zagl"].max()  # doctest: +SKIP
        """
        return self._read("trajectories")


__all__ = ["Simulation"]

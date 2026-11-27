"""
The transport model interface.

A transport model turns one release and one set of transport settings into
a result: a particle table, or another type of result
(:mod:`runtraj.results`). HYSPLIT (:class:`runtraj.models.hysplit.HysplitModel`)
and FLEXPART 11 (:class:`runtraj.models.flexpart.FlexpartModel`) are the
models runtraj supports (:data:`MODELS`); a model in another package is named by its import
path. The worker reaches
a model through :func:`get_model` by the name a run's settings record, so
another model needs no change to the worker.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Self, TypeVar

import pandas as pd
import xarray as xr
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from runtraj._plugins import import_class, near_miss
from runtraj.exceptions import SimulationError
from runtraj.meteorology import run_window
from runtraj.particles import (
    add_release_heights,
    check_particles,
    read_particles,
    write_particles,
)
from runtraj.releases import releases_to_frame
from runtraj.results import model_result, result_type

if TYPE_CHECKING:
    from runtraj.releases import Release
    from runtraj.spatial import Bounds


@dataclass(frozen=True)
class ModelRun:
    """
    What one model run produced.

    Attributes
    ----------
    result : pandas.DataFrame or xarray.DataTree
        What the run made, of the type the model makes with the run's
        config (:mod:`runtraj.results`): the particle table, one row per
        particle per time step (:data:`runtraj.particles.PARTICLE_SCHEMA`),
        or a concentration result (:mod:`runtraj.concentration`).
    log : str
        The model's log of the run, kept with the result
        (:attr:`runtraj.Simulation.log`). Empty for a model that writes none.
    met_files : list of Path
        The meteorology files the run read, recorded in the result's file
        (:func:`runtraj.particles.particles_metadata`). Empty for a model that reads
        no files.
    """

    result: Any
    log: str = ""
    met_files: list[Path] = field(default_factory=list)


class ModelInfo(BaseModel):
    """
    The transport model build a run was made with.

    Its name, the SHA-256 of its executable, and the SHA-256 of each data
    file it reads are part of the run's settings, so two builds
    never share a settings folder. The version the build reports is
    recorded beside the settings (``model_version`` in ``_settings.yaml``),
    not in them: two builds of one release report the same version.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        ...,
        description=(
            "The model as its ``models`` block in ``config.yaml`` names it: "
            "a supported model's name such as ``hysplit``, or the import path of a "
            "model class (the block's ``class``)."
        ),
    )
    sha256: str | None = Field(
        None,
        description=(
            "SHA-256 of the executable the model runs, such as HYSPLIT's "
            "``hycs_std``. ``None`` for a model that runs none."
        ),
    )
    data_files: dict[str, str] | None = Field(
        None,
        description=(
            "SHA-256 of each data file the run read, by file name, such as "
            "HYSPLIT's tables. ``None`` for a model whose build fixes them."
        ),
    )
    version: str | None = Field(
        None,
        exclude=True,
        description=(
            "The version the build reports, such as ``hysplit.v5.1.0``. "
            "Recorded beside the run's settings and left out of them."
        ),
    )


class TransportConfig(BaseModel):
    """
    The base of every transport model's config.

    It holds the parameters runtraj's own code reads, whatever the model:
    the run length and the seed, which set the run window and the
    realizations. Everything else, the particle count included, is the
    model's own. A model's config subclasses it with its own
    parameters and is the model's ``config_class``. In ``config.yaml`` the
    parameters are the keys of the model's block under ``models:``, and a
    variant that runs the model may override any of them. A subclass overrides :meth:`settings` or
    :meth:`realizations` only where its model differs.
    """

    model_config = ConfigDict(extra="forbid")

    #: Fields that change no particle, such as where the build is, left out
    #: of a run's recorded settings.
    UNRECORDED: ClassVar[frozenset[str]] = frozenset()

    n_hours: int = Field(
        description=(
            "Length of each simulation, in hours. Negative runs backward in "
            "time. It has no default: a config must say it."
        ),
    )

    @field_validator("n_hours")
    @classmethod
    def _n_hours_is_not_zero(cls, n_hours: int) -> int:
        """Refuse a run of no hours, which releases particles and follows none."""
        if n_hours == 0:
            raise ValueError(
                "n_hours must not be 0: use a negative number of hours for a "
                "backward run and a positive one for a forward run."
            )
        return n_hours

    seed: int | None = Field(
        None,
        description=(
            "Seed of the model's random numbers, for a reproducible run. "
            "Realization ``k`` of a variant runs with ``seed + k``."
        ),
    )

    def settings(self) -> dict[str, Any]:
        """Return what a run records of this config: every field but the :attr:`UNRECORDED` ones."""
        return self.model_dump(mode="json", exclude=set(self.UNRECORDED))

    def realizations(self, n: int) -> list[Self]:
        """
        Return *n* realizations of this config, realization ``k`` with ``seed + k``.

        Realization 0 uses the configured seed. Without a seed every
        realization is the same config, and the model must draw its own
        random numbers for them to differ.
        """
        seed = self.seed
        return [
            self.model_copy(update={"seed": None if seed is None else seed + k})
            for k in range(n)
        ]


#: The config class a transport model takes, a subclass of :class:`TransportConfig`.
C = TypeVar("C", bound=TransportConfig)

#: The columns of the table ``check_releases`` and :meth:`MetConfig.check` return.
CHECK_COLUMNS = ("check", "release", "message")


class MetConfig(BaseModel):
    """
    The base of every transport model's met config: one entry under ``mets:`` and what is done with its files.

    A model names its subclass as ``met_config_class``; HYSPLIT's is
    :class:`runtraj.models.hysplit.HysplitMetConfig`. The subclass's fields
    are the keys of the entry, and it implements :meth:`settings`. The
    other methods have defaults for a met that names no files, such as a
    model's built-in weather: nothing to check or fetch, nothing
    downloaded, and ARL files when the met is read at the particles. A met
    of files overrides the ones its files need.
    """

    @abstractmethod
    def settings(self) -> dict[str, Any]:
        """
        Return what a run records of this met: which weather it is, and anything else that changes the result.

        Never where its files are kept or how they are named, so that moving
        them changes no settings folder. Plain values (mappings, lists,
        strings, numbers). HYSPLIT's are the source id in the ARL headers
        and the crop.
        """

    def check(self, releases: pd.DataFrame, n_hours: int) -> pd.DataFrame:
        """
        Return what would stop the runs of *releases* on this met, found before any starts; nothing here.

        ``runtraj check`` calls it. *releases* is a table with one row per
        release point (``release``, ``time``, ``longitude``, ``latitude``),
        and *n_hours* the length of the runs. It returns a DataFrame with
        ``check`` (a short name, such as ``met_files``), ``release`` (the id,
        or ``NA`` for a problem of the met itself), and ``message`` columns,
        one row for each problem. It runs and downloads nothing.
        """
        return pd.DataFrame(columns=list(CHECK_COLUMNS))

    def fetch(self, windows: Iterable[tuple[dt.datetime, dt.datetime]]) -> pd.DataFrame:
        """
        Get the files runs of *windows* need before any starts, and return what was got; nothing here.

        ``runtraj fetch`` calls it, with the time each run covers
        (:func:`runtraj.meteorology.run_window`). It gets the files a run of
        those windows would ask for, downloading what the met downloads, and
        returns a DataFrame with ``file`` and ``message`` columns: a row for
        each file, then a row for each window whose files could not be had.
        """
        return pd.DataFrame(columns=["file", "message"])

    def downloads(self) -> bool:
        """
        Return whether this met downloads its files, False here.

        Getting the files once before the runs then saves each run from
        downloading its own, so ``runtraj submit`` submits a fetch job before
        the job array when it is true for a met the unfinished simulations
        use. Reads no file.
        """
        return False

    def open(
        self,
        files: Iterable[str | os.PathLike[str]],
        bounds: Bounds | None = None,
    ) -> list[Path] | xr.Dataset:
        """
        Return *files*, the met files a run read, as :mod:`runtraj.sampling` reads them: as ARL file paths here.

        A met whose files are not ARL files returns them as one
        :class:`xarray.Dataset` in :func:`arlmet.open_dataset`'s layout,
        such as an arlmet converter's output, cropped to *bounds* (a
        :class:`runtraj.Bounds`, or ``None`` for the whole grid). Either can
        be passed to :func:`runtraj.sampling.sample_met` and
        :func:`runtraj.sampling.calc_pbl_height`. The files are a run's
        ``met_files`` (:class:`ModelRun`, or
        :func:`runtraj.particles.particles_metadata`).

        Examples
        --------
        >>> meta = runtraj.particles.particles_metadata(
        ...     sim.particles_path
        ... )  # doctest: +SKIP
        >>> met = variant.met_config.open(meta.met_files)  # doctest: +SKIP
        >>> runtraj.sampling.sample_met(sim.particles, met)  # doctest: +SKIP
        """
        return [Path(f) for f in files]


class TransportModel(ABC, Generic[C]):
    """
    A transport model that follows particles from a release: the class a model subclasses.

    A model sets :attr:`name`, :attr:`config_class` and
    :attr:`met_config_class`, and implements :meth:`run`. Every other
    member has a default here that suits a model which runs one release at
    a time and makes particles, so a model overrides only what it does
    differently: HYSPLIT (:class:`runtraj.models.hysplit.HysplitModel`)
    overrides most, a model in Python that reads no files few. runtraj calls
    each member directly, so a misspelled one (``release_row``) is never
    called; :func:`check_model` reports it.

    It is generic in its config: ``class MyModel(TransportModel[MyConfig])``
    gives :meth:`run` and the other members a ``MyConfig``. It is made with
    no arguments and holds no state.

    Examples
    --------
    >>> from runtraj.models import MetConfig, ModelRun, TransportConfig, TransportModel
    >>> class StillConfig(TransportConfig):
    ...     pass
    >>> class StillMet(MetConfig):
    ...     def settings(self):
    ...         return {}
    >>> class StillModel(TransportModel[StillConfig]):
    ...     name = "still"
    ...     config_class = StillConfig
    ...     met_config_class = StillMet
    ...
    ...     def run(self, release, config, met, window, workdir=None, timeout=None):
    ...         return ModelRun(result=...)
    >>> StillModel().result(StillConfig(n_hours=-24))
    'particles'
    """

    #: The model's short name, such as ``"hysplit"``.
    name: str
    #: The model's config, a :class:`TransportConfig`, which validates its parameters.
    config_class: type[C]
    #: The model's met config, a :class:`MetConfig`, which validates an entry
    #: under ``mets:`` and checks, gets and opens its files. HYSPLIT's is
    #: :class:`runtraj.models.hysplit.HysplitMetConfig`.
    met_config_class: type[MetConfig]
    #: Whether :meth:`run` is handed an empty directory for its files. A
    #: model that writes no files sets it False, and gets ``None``.
    needs_workdir: bool = True
    #: Whether the model runs many releases in one call (:meth:`run_many`),
    #: such as an emulator on a GPU. The worker then hands it every release
    #: of a variant that needs particles at once, where it otherwise calls
    #: :meth:`run` for one at a time.
    batched: bool = False

    @abstractmethod
    def run(
        self,
        release: Release,
        config: C,
        met: Any,
        window: tuple[dt.datetime, dt.datetime],
        workdir: Path | None = None,
        timeout: int | None = None,
    ) -> ModelRun:
        """
        Run one release and return what it made.

        The result is the model's own: runtraj checks it afterwards, and
        adds the release heights to particles (:func:`run_model`).

        Parameters
        ----------
        release : Release
            Where and when particles are released.
        config : TransportConfig
            The model's config, of its ``config_class``.
        met : BaseModel
            The meteorology, of the model's ``met_config_class``. In a
            project its directories are absolute.
        window : tuple of datetime
            The time the run covers, ``(start, end)`` in time order
            (:func:`runtraj.meteorology.run_window`).
        workdir : Path, optional
            An empty directory for the run's files, kept when the run fails.
            ``None`` for a model that does not need one (:attr:`needs_workdir`).
        timeout : int, optional
            Time limit in seconds, for a model that runs a program.
            ``None`` waits indefinitely.

        Raises
        ------
        SimulationError
            When the run fails, with the model's log in ``log``.
        """

    def run_many(
        self,
        releases: list[Release],
        config: C,
        met: Any,
        windows: list[tuple[dt.datetime, dt.datetime]],
        workdir: Path | None = None,
        timeout: int | None = None,
    ) -> ModelRun:
        """
        Run many releases with one config, and return their particles as one table, for a model with :attr:`batched` set.

        The table has a ``release`` column holding each row's release id,
        beside the particle columns :meth:`run` returns. ``windows[i]`` is
        the time ``releases[i]`` covers. A release with no rows failed;
        the others are written. The log and met files are shared by all.

        Raises
        ------
        SimulationError
            When the whole call fails, with the model's log in ``log``.
        NotImplementedError
            Here, for a model that is not batched.
        """
        raise NotImplementedError(
            f"Transport model {self.name!r} sets batched but has no run_many."
        )

    def result(self, config: C) -> str:
        """
        Return the type of result the model makes with *config*: ``particles`` here.

        A name in :data:`runtraj.results.RESULT_TYPES`, such as
        ``"concentration"``. A variant's stages read particles, so a variant
        whose model makes another type has none.
        """
        return "particles"

    def result_files(self, config: C) -> tuple[str, ...]:
        """
        Return the names of the files in a workdir that hold a run's result, none here.

        Such as HYSPLIT's particle file or its ``cdump_<grid>`` files. A
        failed run's workdir is kept without them, since they can be large
        and are read into the result anyway; ``keep_workdir: true`` keeps
        them. With none, every file is kept.
        """
        return ()

    def sha256(self, config: C) -> str | None:
        """
        Return the SHA-256 of the executable *config* would run, ``None`` here, for a model that runs none.

        Part of the run's identity: another build gives another settings
        folder. Raises ``FileNotFoundError`` when the executable cannot be
        found here; a project still opens, and its status compares stored
        folders without the build.
        """
        return None

    def version(self, config: C) -> str | None:
        """Return the version the build *config* would run reports, recorded with the run but not part of its identity; ``None`` here."""
        return None

    def data_files(self, config: C) -> dict[str, str] | None:
        """
        Return the checksum of each data file a run with *config* reads, by file name; ``None`` here.

        ``None`` for a model whose build fixes its data. These are part of
        the run's identity, since the data change the particles.
        """
        return None

    def check_releases(self, releases: pd.DataFrame, config: C) -> pd.DataFrame:
        """
        Return the releases the model cannot run with *config*, one row each; none here.

        *releases* is a table with one row per release point (``release``,
        ``kind``, ``longitude``, ``latitude``, ``altitude``,
        ``altitude_ref``). It returns a DataFrame with ``check``,
        ``release`` and ``message`` columns, in that order, one row for each
        release it cannot run. ``runtraj check`` lists them, and
        :func:`run_model` and :func:`run_model_many` fail them with the
        reason ``RELEASE_REFUSED`` before the model runs, so :meth:`run`
        and :meth:`run_many` never get such a release. HYSPLIT refuses a
        multipoint release whose points share a horizontal location, which
        it would join into a line source.
        """
        return pd.DataFrame(columns=list(CHECK_COLUMNS))

    def release_rows(
        self, particles: pd.DataFrame, release: Release, config: C
    ) -> pd.DataFrame | None:
        """
        Return where each particle of a column or multipoint release started, ``None`` here.

        For a model that writes no row at ``age = 0``: one row per particle
        with ``particle``, ``lon``, ``lat``, and ``height`` in the release's
        vertical reference (:func:`runtraj.particles.add_release_heights`),
        from the particles and the *config* of the run that made them.
        A model that writes release rows needs none; one that writes
        neither cannot run a column or multipoint release.
        """
        return None


def _model_hooks() -> set[str]:
    """Return the names of :class:`TransportModel`'s members: its hooks and attributes."""
    return {
        name
        for name in (*vars(TransportModel), *TransportModel.__annotations__)
        if not name.startswith("_")
    }


def _hysplit() -> TransportModel:
    """Return HYSPLIT, importing its package only when it is asked for."""
    from runtraj.models.hysplit import HysplitModel

    return HysplitModel()


def _flexpart() -> TransportModel:
    """Return FLEXPART, importing its package only when it is asked for."""
    from runtraj.models.flexpart import FlexpartModel

    return FlexpartModel()


#: The transport models runtraj supports, by the name of their block under
#: ``models:`` in ``config.yaml``: ``hysplit``, and ``flexpart``, which runs
#: a FLEXPART 11 checkout you built. Any other model is named
#: by its import path instead, the ``class`` of its block.
MODELS: dict[str, Callable[[], TransportModel]] = {
    "hysplit": _hysplit,
    "flexpart": _flexpart,
}


def get_model(name: str) -> TransportModel:
    """
    Return the transport model *name* names.

    *name* is a model runtraj supports (:data:`MODELS`, such as ``hysplit``) or the
    import path of a model class, such as ``emulator.runtraj.EmulatorModel``,
    which is imported and made with no arguments. A model in a package of
    its own needs no registration, so every process that opens the project
    finds it.

    Raises
    ------
    ValueError
        If *name* is not a model runtraj supports and not an import path, or the
        class at the import path does not subclass :class:`TransportModel`
        or lacks one of its required members.
    MissingClass
        If *name* is an import path that cannot be imported on this machine
        (an :class:`ImportError`).
    """
    if "." in name:
        cls = import_class(name)
        if not (isinstance(cls, type) and issubclass(cls, TransportModel)):
            raise ValueError(
                f"{name} is not a transport model: its class must subclass "
                "runtraj.models.TransportModel."
            )
        missing = [
            member
            for member in ("name", "config_class", "met_config_class")
            if not hasattr(cls, member)
        ] + sorted(cls.__abstractmethods__)
        if missing:
            raise ValueError(
                f"{name} is not a transport model: it has no {', '.join(missing)}."
            )
        met_class = cls.met_config_class
        if not (isinstance(met_class, type) and issubclass(met_class, MetConfig)):
            raise ValueError(
                f"{name}'s met_config_class must subclass runtraj.models.MetConfig."
            )
        return cls()
    factory = MODELS.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown transport model {name!r}. The models runtraj supports are "
            f"{sorted(MODELS)}; another is named by its import path, such as "
            "mypkg.models.MyModel."
        )
    return factory()


def run_model(
    name: str,
    release: Release,
    config: TransportConfig,
    met: Any,
    workdir: Path | None = None,
    timeout: int | None = None,
) -> ModelRun:
    """
    Run the transport model called *name* for one release, and finish its result.

    The result is checked against its type (:mod:`runtraj.results`).
    Particles are checked against the particle table
    (:func:`runtraj.particles.check_particles`) and get their release heights
    (:func:`runtraj.particles.add_release_heights`). These steps are
    runtraj's, the same for any model. Nothing else is added: ``foot`` is
    the model's own, or missing for a model that writes none.

    Parameters
    ----------
    name : str
        The transport model (:data:`MODELS`).
    release : Release
        Where and when particles are released.
    config : TransportConfig
        The model's config.
    met : BaseModel
        The meteorology, of the model's ``met_config_class``.
    workdir : Path, optional
        An empty directory for the run's files. ``None`` lets the model make
        its own.
    timeout : int, optional
        Time limit in seconds.

    Raises
    ------
    SimulationError
        If the model's ``check_releases`` refuses the release
        (``RELEASE_REFUSED``), before it runs; if the run fails, as when the
        meteorology does not cover it (``MET_COVERAGE``), or the model wrote
        no result (``NO_PARTICLE_DATA``, ``NO_CONCENTRATION_DATA``); or if where the particles of a column or multipoint
        release started is not known (``NO_RELEASE_ROWS``).
    """
    return _run_one(get_model(name), release, config, met, workdir, timeout)


def _run_one(
    model: TransportModel,
    release: Release,
    config: TransportConfig,
    met: Any,
    workdir: Path | None,
    timeout: int | None,
) -> ModelRun:
    """Run *model* for *release* as :func:`run_model` does: refuse, run, finish."""
    refused = _refused(model, [release], config).get(str(release.id))
    if refused is not None:
        raise refused
    window = run_window(release.time, config.n_hours)
    run = _returned(model.run(release, config, met, window, workdir, timeout=timeout))
    return _finish(run, release, model, config)


def run_model_many(
    name: str,
    releases: list[Release],
    config: TransportConfig,
    met: Any,
    workdir: Path | None = None,
    timeout: int | None = None,
) -> dict[str, ModelRun | SimulationError]:
    """
    Run a batched transport model once for many releases, and finish each one's particles.

    The model's one table is split by its ``release`` column, and each
    release's particles get the steps :func:`run_model` applies. A release
    the model's ``check_releases`` refuses is left out of the call and
    gets a ``RELEASE_REFUSED`` error; one whose particles are missing or
    fail those steps gets its error, and the others their runs.

    Returns
    -------
    dict
        ``{release id: ModelRun}``, or the :class:`SimulationError` of a
        release that failed.

    Raises
    ------
    SimulationError
        If the whole call fails.
    """
    return _run_many(get_model(name), releases, config, met, workdir, timeout)


def _run_many(
    model: TransportModel,
    releases: list[Release],
    config: TransportConfig,
    met: Any,
    workdir: Path | None,
    timeout: int | None,
) -> dict[str, ModelRun | SimulationError]:
    """Run batched *model* for *releases* as :func:`run_model_many` does."""
    made = model_result(model, config)
    if made != "particles":
        raise TypeError(
            f"Transport model {model.name!r} is batched and makes {made}: a "
            "batched model returns one particle table for many releases, so it "
            "makes particles."
        )
    results: dict[str, ModelRun | SimulationError] = dict(
        _refused(model, releases, config)
    )
    releases = [r for r in releases if str(r.id) not in results]
    if not releases:
        return results
    windows: list[tuple[dt.datetime, dt.datetime]] = [
        run_window(r.time, config.n_hours) for r in releases
    ]
    run = _returned(
        model.run_many(releases, config, met, windows, workdir, timeout=timeout)
    )
    if not isinstance(run.result, pd.DataFrame) or "release" not in run.result.columns:
        raise SimulationError(
            "A batched transport model's particles need a release column.",
            reason="NO_PARTICLE_DATA",
            log=run.log,
        )
    by_release = dict(tuple(run.result.groupby("release", sort=False)))
    for release in releases:
        rows = by_release.get(str(release.id))
        own = replace(
            run,
            result=(
                pd.DataFrame()
                if rows is None
                else rows.drop(columns="release").reset_index(drop=True)
            ),
        )
        try:
            results[str(release.id)] = _finish(own, release, model, config)
        except SimulationError as error:
            error.log = run.log
            results[str(release.id)] = error
    return results


def _returned(run: Any) -> ModelRun:
    """Return *run*, what a model's ``run`` or ``run_many`` returned, when it is a :class:`ModelRun`."""
    if not isinstance(run, ModelRun):
        raise TypeError(
            f"A transport model's run must return a ModelRun, not {type(run).__name__}."
        )
    return run


def _release_points(releases: Iterable[Release]) -> pd.DataFrame:
    """
    Return *releases* as a model's ``check_releases`` gets them: one row per release point.

    The columns are ``release`` (the release id), ``kind``, ``longitude``,
    ``latitude``, ``altitude`` and ``altitude_ref``, the columns of a
    project's release rows that a model reads.
    """
    releases = list(releases)
    rows = releases_to_frame(releases)
    each = [r for r in releases for _ in r.coords()]  # a release per point
    rows["release"] = [str(r.id) for r in each]
    rows["kind"] = [r.kind for r in each]
    return rows[
        ["release", "kind", "longitude", "latitude", "altitude", "altitude_ref"]
    ]


def _refused(
    model: TransportModel, releases: list[Release], config: TransportConfig
) -> dict[str, SimulationError]:
    """Return the error of each of *releases* the model's ``check_releases`` refuses, by release id."""
    ids = {str(r.id) for r in releases}
    table = model.check_releases(_release_points(releases), config)
    return {
        str(release): SimulationError(str(message), reason="RELEASE_REFUSED")
        for _check, release, message in table.itertuples(index=False)
        if str(release) in ids
    }


def _finish(
    run: ModelRun, release: Release, model: TransportModel, config: TransportConfig
) -> ModelRun:
    """
    Return *run* with its result checked, and particles with their release heights.

    Particles that stop before the end of the run are kept: the transport
    model fails a run cut short by its meteorology itself, and particles
    that all left the met's domain are a complete run. The particles keep
    the columns the model wrote: ``foot`` is the model's own, or missing.
    Another type of result is checked by its type (:mod:`runtraj.results`).

    Raises
    ------
    SimulationError
        With the run's log, if the model wrote no particles
        (``NO_PARTICLE_DATA``), or another type of result that its type
        refuses (``NO_CONCENTRATION_DATA``, ...), or where each particle of
        a column or multipoint release started is not known.
    """
    made = model_result(model, config)
    if made != "particles":
        try:
            result_type(made).check(run.result)
        except ValueError as error:
            raise SimulationError(
                str(error), reason=f"NO_{made.upper()}_DATA", log=run.log
            ) from error
        return run
    if not isinstance(run.result, pd.DataFrame) or run.result.empty:
        raise SimulationError(
            "The transport model wrote no particles.",
            reason="NO_PARTICLE_DATA",
            log=run.log,
        )
    check_particles(run.result)
    try:
        rows = model.release_rows
        particles = add_release_heights(
            run.result, release, None if rows is None else partial(rows, config=config)
        )
    except ValueError as error:
        raise SimulationError(
            str(error), reason="NO_RELEASE_ROWS", log=run.log
        ) from error
    return replace(run, result=particles)


def run_trajectories(
    release: Release,
    met: BaseModel | Mapping[str, Any],
    *,
    model: str,
    workdir: str | Path | None = None,
    timeout: int | None = None,
    **params: Any,
) -> Any:
    """
    Run the transport model for one release and return what it made, particles unless the settings say another type.

    This is what a project runs for each release, without a project: the
    result is returned, not stored. Its type follows the settings, as a
    variant's does (:mod:`runtraj.results`): particles, or with HYSPLIT's
    ``mode="trajectory"`` the trajectory table, or with
    ``mode="concentration"`` the grids.

    Parameters
    ----------
    release : Release
        Where and when particles are released.
    met : str, dict or BaseModel
        The meteorology, as one entry under ``mets:`` in ``config.yaml``,
        checked by the model's met config
        (:class:`runtraj.models.hysplit.HysplitMetConfig` for HYSPLIT). A string is
        the folder of its files, and a relative folder starts from the
        working directory.
    model : str
        The transport model: one runtraj supports (:data:`MODELS`, such as
        ``"hysplit"``) or the import path of a model class. There is no
        default.
    workdir : str or Path, optional
        Directory the model runs in, kept afterwards for a look at its
        files. It must be empty or not exist yet. Without it the model runs
        in a temporary directory that is removed.
    timeout : int, optional
        Time limit in seconds. ``None`` waits indefinitely.
    **params
        The transport model's parameters, such as ``n_hours`` and
        HYSPLIT's ``numpar`` (:class:`runtraj.models.hysplit.HysplitConfig`).
        ``n_hours`` is required; the others default to the model's own
        values.

    Returns
    -------
    pandas.DataFrame or xarray.DataTree
        The particle table, one row per particle per output step, with
        ``time`` (UTC), the release time plus ``age``, as a table read from
        a particle file has. ``foot`` is the model's own (HYSPLIT's, made
        with ``veght``). A model that writes no ``foot`` returns none.
        With settings that make another type, that type's result: the
        trajectory table (:mod:`runtraj.trajectories`), or a tree of
        concentration grids (:mod:`runtraj.concentration`).

    Raises
    ------
    ValueError
        If *workdir* is not empty, or a parameter is not one of the
        model's.
    SimulationError
        If the run fails; ``reason`` says why.
    MeteorologyError
        If the met files the run needs are missing.

    Examples
    --------
    >>> release = runtraj.PointRelease(  # doctest: +SKIP
    ...     time="2023-07-15 18:00", longitude=-111.848, latitude=40.766, altitude=10
    ... )
    >>> particles = runtraj.run_trajectories(
    ...     release,
    ...     "/data/hrrr",
    ...     model="hysplit",
    ...     n_hours=-24,
    ...     numpar=200,  # HYSPLIT's particle count
    ... )  # doctest: +SKIP
    >>> table = runtraj.run_trajectories(
    ...     release, "/data/hrrr", model="hysplit", n_hours=-24, mode="trajectory"
    ... )  # doctest: +SKIP
    """
    transport_model = get_model(model)
    config = transport_model.config_class(**params)
    made = model_result(transport_model, config)
    met_config = transport_model.met_config_class.model_validate(met)
    if workdir is not None:
        workdir = Path(workdir)
        if workdir.exists() and any(workdir.iterdir()):
            raise ValueError(
                f"{workdir} is not empty. The model would read files left from "
                "another run; give an empty or new directory."
            )
        workdir.mkdir(parents=True, exist_ok=True)
    particles = run_model(model, release, config, met_config, workdir, timeout).result
    if made != "particles":
        return particles
    particles["time"] = pd.Timestamp(release.time) + pd.to_timedelta(
        particles["age"].to_numpy(), unit="min"
    )
    return particles


#: Keys a run's settings record holds beside the transport model's own
#: settings (:func:`runtraj.identity.run_settings`), or a variant reads as its
#: own (:class:`runtraj.config.Variant`): a model's config may not use them.
RESERVED_SETTINGS = frozenset(
    {"met", "model", "ensemble", "realization", "realizations", "stages"}
)


def check_model(
    model: TransportModel,
    config: TransportConfig,
    met: Any = None,
    release: Release | None = None,
) -> None:
    """
    Check that *model* keeps the promises runtraj relies on, for one *config*.

    Call it from the tests of a transport model of your own, as
    :func:`runtraj.stages.check_stage` is for a stage. It checks that:

    - *model* subclasses :class:`TransportModel` and sets ``name``, and no
      public name of its class is spelled almost like a member of the base
      class, as ``release_row`` is, which runtraj would never call;
    - ``config_class`` is a :class:`TransportConfig` whose fields use none
      of :data:`RESERVED_SETTINGS`, and ``met_config_class`` a :class:`MetConfig`;
    - the record ``config.settings()`` writes is plain data, and
      ``config_class`` reads it back to a config with the same record: a
      stored settings folder is found that way; and so for each config
      ``config.realizations(3)`` returns, unless it refuses with a
      ``ValueError``;
    - ``sha256``, ``version`` and ``data_files`` make a :class:`ModelInfo`
      (``sha256`` may raise ``FileNotFoundError`` when the build is not on
      this machine).

    With a *release* and its *met* (of ``met_config_class``), it also runs
    the model once, through the code a project's run uses (``run``, and
    ``run_many`` too for a batched model), and checks the result. A result
    of a type other than particles (the model's ``result``) is checked by
    its type and stored and read back. Particles are checked further: the
    columns of the particle table, ages that go the way ``n_hours`` does and
    stay within it, the release heights of a column or multipoint release
    (from a row at ``age = 0`` or the model's ``release_rows``), and a
    particle file that stores every column. A model's ``check_releases``
    must return its table's columns in order; when it refuses *release*, the
    run must fail as ``RELEASE_REFUSED``, and nothing is run. Run it with a
    point, a column and a multipoint release to cover each.

    Raises
    ------
    AssertionError
        Saying which promise is broken.
    SimulationError
        If the run fails, as it would in a project.

    Examples
    --------
    >>> from runtraj.models import check_model
    >>> from runtraj.models.hysplit import HysplitConfig, HysplitModel
    >>> check_model(HysplitModel(), HysplitConfig(n_hours=-24))
    """
    name = type(model).__name__
    assert isinstance(model, TransportModel), (
        f"{name} must subclass runtraj.models.TransportModel"
    )
    assert isinstance(model.name, str) and model.name, f"{name}.name must be a string"
    miss = near_miss(type(model), _model_hooks())
    assert miss is None, miss
    assert isinstance(model.config_class, type) and issubclass(
        model.config_class, TransportConfig
    ), f"{name}.config_class must subclass runtraj.models.TransportConfig"
    reserved = sorted(RESERVED_SETTINGS & set(model.config_class.model_fields))
    assert not reserved, (
        f"{name}.config_class may not have a field named {reserved}: runtraj "
        "records or reads those keys beside the model's settings"
    )
    assert isinstance(model.met_config_class, type) and issubclass(
        model.met_config_class, MetConfig
    ), f"{name}.met_config_class must subclass runtraj.models.MetConfig"
    assert isinstance(config, model.config_class), (
        f"config must be a {model.config_class.__name__}"
    )
    _check_reads_back(model, config)
    try:
        realizations = config.realizations(3)
    except ValueError:
        realizations = []  # a config may refuse realizations, as without a seed
    else:
        assert len(realizations) == 3, f"{name}'s realizations(3) must return 3 configs"
    for each in realizations:
        assert isinstance(each, model.config_class), (
            f"{name}'s realizations() must return {model.config_class.__name__}s"
        )
        _check_reads_back(model, each)
    _check_build(model, config)
    if release is None:
        return
    assert isinstance(met, model.met_config_class), (
        f"met must be a {model.met_config_class.__name__} to run the model"
    )
    _plain(met.settings(), f"{name}'s met settings()")
    if not _check_refusal(model, config, met, release):
        _check_run(model, config, met, release)


def _plain(record: Any, what: str) -> dict[str, Any]:
    """Return *record* when it is a mapping of plain data, as a settings file stores it."""
    from runtraj.identity import canonical

    assert isinstance(record, Mapping), f"{what} must return a mapping"
    try:
        return json.loads(json.dumps(canonical(record)))
    except TypeError as error:
        raise AssertionError(
            f"{what} must return plain data (mappings, lists, strings, numbers): "
            f"{error}"
        ) from None


def _check_reads_back(model: TransportModel, config: TransportConfig) -> None:
    """Check *config*'s settings record is plain data its class reads back."""
    name = type(model).__name__
    record = _plain(config.settings(), f"{name}'s config settings()")
    fields = model.config_class.model_fields
    try:
        read_back = model.config_class.model_validate(
            {k: v for k, v in record.items() if k in fields}
        )
    except ValidationError as error:
        raise AssertionError(
            f"{name}.config_class cannot read back what settings() wrote: {error}"
        ) from None
    again = _plain(read_back.settings(), f"{name}'s config settings()")
    assert again == record, (
        f"{name}.config_class does not read back what settings() wrote: "
        f"{record} became {again}"
    )


def _check_build(model: TransportModel, config: TransportConfig) -> None:
    """Check the model's build makes a :class:`ModelInfo`, as a variant builds it."""
    name = type(model).__name__
    try:
        sha256 = model.sha256(config)
        data_files = model.data_files(config)
    except FileNotFoundError:
        return  # the build is not on this machine, which a project allows
    try:
        ModelInfo(
            name=model.name,
            sha256=sha256,
            data_files=data_files,
            version=model.version(config),
        )
    except ValidationError as error:
        raise AssertionError(
            f"{name}'s sha256, data_files and version must make a ModelInfo: {error}"
        ) from None


def _check_refusal(
    model: TransportModel, config: TransportConfig, met: Any, release: Release
) -> bool:
    """Check the model's ``check_releases`` table, and return whether it refuses *release*."""
    name = type(model).__name__
    table = model.check_releases(_release_points([release]), config)
    assert isinstance(table, pd.DataFrame) and list(table.columns) == [
        "check",
        "release",
        "message",
    ], f"{name}.check_releases must return a table of check, release and message"
    if str(release.id) not in set(table["release"].astype(str)):
        return False
    try:
        _run_one(model, release, config, met, None, None)
    except SimulationError as error:
        assert error.reason == "RELEASE_REFUSED", (
            f"a release {name}.check_releases lists must fail as RELEASE_REFUSED"
        )
        return True
    raise AssertionError(f"a release {name}.check_releases lists must not run")


def _check_run(
    model: TransportModel, config: TransportConfig, met: Any, release: Release
) -> None:
    """Run *model* once for *release* as a project's run does, and check its particles."""
    name = type(model).__name__
    batched = model.batched
    assert not batched or type(model).run_many is not TransportModel.run_many, (
        f"{name} is batched, so it needs run_many"
    )
    with tempfile.TemporaryDirectory(prefix="runtraj-check-") as tmp:
        workdir = None
        if model.needs_workdir:
            workdir = Path(tmp) / "work"
            workdir.mkdir()
        runs = {"run": _promised(name, _run_one, model, release, config, met, workdir)}
        if batched:
            # The worker gives run_many no workdir.
            many = _promised(name, _run_many, model, [release], config, met, None)
            result = many.get(str(release.id))
            assert result is not None, f"{name}.run_many must return the release"
            if isinstance(result, SimulationError):
                raise result
            runs["run_many"] = result
        made = model_result(model, config)
        for how, run in runs.items():
            if made == "particles":
                _check_particles(f"{name}.{how}", run, config, release, Path(tmp))
            else:
                _check_stored(f"{name}.{how}", run, made, config, release, Path(tmp))


def _promised(name: str, run: Callable[..., Any], *args: Any) -> Any:
    """Return ``run(*args, None)``, a run as the core makes it, with a broken promise as an :class:`AssertionError`."""
    try:
        return run(*args, None)
    except (TypeError, ValueError) as error:
        raise AssertionError(f"{name}: {error}") from error


def _check_particles(
    what: str, run: ModelRun, config: TransportConfig, release: Release, tmp: Path
) -> None:
    """Check the finished particles of one run, and that a particle file stores them."""
    assert isinstance(run.log, str), f"{what}'s ModelRun.log must be a string"
    finished = run.result
    age = finished["age"].to_numpy(dtype=float)
    backward = config.n_hours < 0
    assert ((age <= 0) if backward else (age >= 0)).all(), (
        f"{what}'s ages must be {'<=' if backward else '>='} 0 for "
        f"n_hours {config.n_hours}: minutes since release"
    )
    assert (abs(age) <= abs(config.n_hours) * 60).all(), (
        f"{what}'s ages must stay within n_hours ({config.n_hours} h)"
    )
    if "release_height" in finished.columns:
        assert finished["release_height"].notna().all(), (
            f"every particle of {what} needs a release height"
        )
    path = tmp / "particles.parquet"
    try:
        write_particles(path, finished, release, config.settings(), run.met_files)
    except ValueError as error:
        raise AssertionError(f"{what}'s particles cannot be stored: {error}") from error
    missing = set(finished.columns) - set(read_particles(path).columns)
    assert not missing, f"a particle file does not store {what}'s {sorted(missing)}"
    path.unlink()


def _check_stored(
    what: str,
    run: ModelRun,
    made: str,
    config: TransportConfig,
    release: Release,
    tmp: Path,
) -> None:
    """Check that a result of the type *made*, not particles, is stored and read back by its type."""
    assert isinstance(run.log, str), f"{what}'s ModelRun.log must be a string"
    kind = result_type(made)
    path = tmp / f"result{kind.suffix}"
    try:
        kind.write(path, run.result, release, config.settings(), run.met_files)
    except ValueError as error:
        raise AssertionError(f"{what}'s {made} cannot be stored: {error}") from error
    kind.check(kind.read(path))
    path.unlink()


__all__ = [
    "MODELS",
    "ModelInfo",
    "MetConfig",
    "ModelRun",
    "TransportConfig",
    "TransportModel",
    "RESERVED_SETTINGS",
    "check_model",
    "get_model",
    "run_model",
    "run_model_many",
    "run_trajectories",
]

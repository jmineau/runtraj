"""
The transport model contract: what a model in another package subclasses.

:class:`TransportModel` is the model, typed by its :class:`TransportConfig`;
:class:`MetConfig` checks one entry under ``mets:`` and owns what is done with
its files; :class:`ModelRun` is what a run returns. Running a model and
finding it by name are in :mod:`runtraj.models.run` and
:mod:`runtraj.models.registry`; :func:`runtraj.testing.check_model` checks a
model against this contract.
"""

from __future__ import annotations

import datetime as dt
import os
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Self, TypeVar

import pandas as pd
import xarray as xr
from pydantic import BaseModel, ConfigDict, Field, field_validator

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

    Every met has :attr:`label`, which a run records beside :meth:`settings`
    when it is set (:attr:`runtraj.config.Variant.met_settings`).
    """

    label: str | None = Field(
        None,
        description=(
            "A name that tells this met's runs apart from those of another met "
            "of the same weather product, such as ``mynn`` and ``ysu`` for two "
            "WRF runs with different boundary-layer schemes, whose ARL files "
            "carry the same source id. A run records it only when it is set, so "
            "a met without one keeps its settings folders, and setting one "
            "moves its runs to a new folder. ``runtraj check`` and a run refuse "
            "two mets of one transport model that record the same settings and "
            "no label to tell them apart, since their variants would share one "
            "run."
        ),
    )

    @abstractmethod
    def settings(self) -> dict[str, Any]:
        """
        Return what a run records of this met: which weather it is, and anything else that changes the result.

        Never where its files are kept or how they are named, so that moving
        them changes no settings folder. Plain values (mappings, lists,
        strings, numbers). HYSPLIT's are the source id in the ARL headers
        and the crop. The run adds :attr:`label` when it is set, so a
        subclass leaves it out.
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
        ``kind``, ``time``, ``longitude``, ``latitude``, ``altitude``,
        ``altitude_ref``). It returns a DataFrame with ``check``,
        ``release`` and ``message`` columns, in that order, one row for each
        release it cannot run; a row whose ``release`` is ``NA`` is about the
        settings, and refuses every release. ``runtraj check`` lists them, and
        :func:`run_model` fails them with the reason ``RELEASE_REFUSED``
        before the model runs, so :meth:`run` never gets such a release. HYSPLIT refuses a
        multipoint release whose points share a horizontal location, which
        it would join into a line source, in its trajectory mode with
        a ``hyts_std`` before 5.4.0 a release not on the hour, and a setting
        its ``hycs_std`` would ignore (HYSPLIT's ``krand`` 10 to 13 before
        5.1.0, the wind error in the concentration mode from 5.3.0).
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


#: Keys a run's settings record holds beside the transport model's own
#: settings (:func:`runtraj.identity.run_settings`), or a variant reads as its
#: own (:class:`runtraj.config.Variant`): a model's config may not use them.
RESERVED_SETTINGS = frozenset(
    {"met", "model", "ensemble", "realization", "realizations", "stages"}
)

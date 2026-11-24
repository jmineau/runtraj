"""
The stage interface: what is made from each simulation's results after the transport model runs.

A stage makes one file per release from the particles, from the files of
other stages, or both, and writes that file itself. PYSTILT's footprint is
one (``stilt.footprint.FootprintStage``). A project lists its stages under
``stages:`` in ``config.yaml``, each block naming the stage's class by its
import path, so the worker runs a stage without importing it by name and a
stage in another package needs no registration:

.. code-block:: yaml

   stages:
     footprint:
       class: stilt.footprint.FootprintStage
       grid: {xmin: -113.0, xmax: -110.5, ymin: 40.0, ymax: 42.0, xres: 0.01, yres: 0.01}

:class:`Stage` is the class such a class subclasses. :func:`get_stage`
imports one, :func:`ordered` puts the stages of a config in the order they
run, and :func:`check_stage` is for the tests of a stage of your own.
"""

from __future__ import annotations

import functools
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel

from runtraj._plugins import import_class, near_miss
from runtraj.identity import canonical
from runtraj.results import RESULT_TYPES

if TYPE_CHECKING:
    import pandas as pd
    from upath import UPath

    from runtraj.simulation import Simulation

#: Pattern for a stage's name, which is a folder in the output directory and a column of ``project.status()``.
STAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Names a stage may not take: the output directory's own trees (the result
#: types of :mod:`runtraj.results`), and the columns of ``project.status()``.
RESERVED_NAMES = frozenset(
    {
        "particles",
        "concentration",
        "trajectories",
        "logs",
        "scratch",
        "release",
        "variant",
        "realization",
        "model",
        "state",
        "stage",
        "reason",
        "message",
    }
)


#: The config class a stage takes, a pydantic model.
C = TypeVar("C", bound=BaseModel)

#: Pattern for a stage's ``version``: letters, digits, dots, dashes and underscores.
STAGE_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class Stage(ABC, Generic[C]):
    """
    What is made from each simulation's results, one file per release: the class a stage subclasses.

    A stage sets :attr:`name`, :attr:`config_class` and :attr:`suffix`, and
    implements :meth:`run`; :meth:`resolve`, :meth:`settings`,
    :meth:`check`, :attr:`reads`, :attr:`result` and :attr:`version` have
    defaults here. runtraj calls each member directly, so a misspelled one
    (``read``) is never called; :func:`check_stage` reports it. It is
    generic in its config, ``class MyStage(Stage[MyConfig])``,
    and is made with no arguments and holds no state: everything a stage
    needs is in its config and the simulation it is handed.

    The stage's :attr:`config_class` has two optional members of its own. A
    classmethod ``override(block, own)`` returns a variant's settings from
    the block's, as the class dumps them, and the variant's own, as
    written; without it the variant's keys replace the block's. A property
    ``zsfc_reasons`` lists what goes wrong for releases above sea level
    whose particles lack the terrain height ``zsfc``, a clause each, for
    ``runtraj check`` to report when the transport model is not asked for
    it.
    """

    #: What the stage's results are called, such as ``footprint``: the name
    #: of its block under ``stages:``, its tree in the output directory, its
    #: column in ``project.status()``, the ``stage`` of a failure record, and
    #: a part of the hash that names its settings folders. Lowercase
    #: letters, digits and underscores.
    name: str
    #: Checks the stage's block of ``config.yaml`` when the file loads, and
    #: reads no file. It must also read back what :meth:`settings` wrote,
    #: since a stored settings folder is found by validating its record with
    #: this class and hashing :meth:`settings` again (:func:`check_stage`
    #: holds a stage to that).
    config_class: type[C]
    #: The extension of the stage's files, such as ``.parquet``, with its
    #: dot. The file of a release is ``<release id><suffix>``, and completion
    #: looks for it in a listing.
    suffix: str
    #: The names of the stages whose files this one reads, none here. A
    #: stage runs after the ones it reads, and again whenever one of them
    #: was made again.
    reads: tuple[str, ...] = ()
    #: The type of transport model result the stage reads
    #: (:mod:`runtraj.results`), ``particles`` here. A variant whose model
    #: makes another type cannot use the stage.
    result: str = "particles"
    #: The version of what the stage makes, ``None`` here. When set, it is
    #: part of the hash that names the stage's settings folders and is
    #: recorded as ``stage_version`` in their ``_settings.yaml``, so a fix
    #: that changes the stage's files makes new ones under a new folder
    #: instead of counting the old ones complete. Raise it only for such a
    #: fix; a stage that never sets it keeps the folders it has.
    version: str | None = None

    def resolve(
        self,
        config: C,
        model: str,
        model_settings: Mapping[str, Any],
        directory: Path | None,
    ) -> C:
        """
        Return the config a variant's simulations run with, from the one ``config.yaml`` gives.

        Called once for each variant when a project reads its variants. This
        is where the stage reads the files its settings name, and where it
        refuses a transport model whose particles it cannot use.

        Parameters
        ----------
        config : config_class
            The variant's settings of this stage, checked.
        model : str
            The variant's transport model, as its ``models`` block names it.
        model_settings : mapping
            The model's settings as a run records them.
        directory : Path or None
            The project directory, where a relative file name starts.

        Raises
        ------
        ValueError
            If the settings do not fit the model, or name a file that
            cannot be used.

        Here *config* is returned as it is.
        """
        return config

    def settings(self, config: C) -> dict[str, Any]:
        """
        Return what identifies the stage's results, besides what it reads: the record of its settings folder.

        Plain values (mappings, lists, strings, numbers), which
        :attr:`config_class` reads back. The folder's hash is of this
        record, the particles' hash, and the hash of each stage in
        :attr:`reads`. Here, every field of *config* (``model_dump``).
        """
        return config.model_dump(mode="json")

    def check(
        self, config: C, releases: pd.DataFrame, directory: Path | None
    ) -> list[tuple[str, str | None, str]]:
        """
        Return what would stop this stage for the project's releases, found without running anything.

        ``project.check()`` and ``runtraj check`` call it. *releases* has one
        row per release point (``release``, ``time``, ``longitude``,
        ``latitude``, ``altitude``, ``altitude_ref``, ``kind``). Each thing
        found is a tuple of a short name for the check, the release id (or
        ``None`` when it is not about one release), and a message. Nothing
        here.
        """
        return []

    @abstractmethod
    def run(self, config: C, sim: Simulation, path: Path | UPath) -> None:
        """
        Make the stage's file for one simulation, and write it at *path*.

        The worker calls it for each simulation whose file is missing, after
        the particles and the files of the stages in :attr:`reads` exist.
        Read the particles from ``sim.particles`` (a pandas DataFrame, which
        simulations that share it hold in common: do not change it in
        place), and another stage's file from ``sim.path("<its name>")``
        with that stage's own reader. Write one file at *path* and nothing
        else, whatever there is to say: a result with nothing in it is still
        a file. On a filesystem *path* is a
        temporary name beside the file's own, ending in :attr:`suffix`,
        which the worker moves into place when this returns; a file left
        by a run that raises is removed. So a run cut off part-way leaves
        no file that counts as complete. A Parquet file may carry
        :func:`runtraj.identity.file_stamp` in its metadata, as runtraj's own
        files do.

        Parameters
        ----------
        config : config_class
            The variant's settings of this stage, as :meth:`resolve`
            returned them.
        sim : Simulation
            The simulation: its release, its variant, its particles, and
            the project directory (``sim.directory``), where a relative file
            name in the settings starts.
        path : Path or UPath
            Where to write the file: a :class:`pathlib.Path`, or a
            :class:`upath.UPath` when the output directory is on an object
            store. Write through ``path.open("wb")`` (or ``"w"``), which
            both have, and the stage works with either.

        Raises
        ------
        Exception
            Any error fails the simulation at this stage: the worker records
            it (:attr:`runtraj.Simulation.failure`) and goes on. Raise a
            :class:`~runtraj.exceptions.SimulationError` with a ``reason`` for
            a failure you expect.
        """


def _stage_hooks() -> set[str]:
    """Return the names of :class:`Stage`'s members: its hooks and attributes."""
    return {
        name
        for name in (*vars(Stage), *Stage.__annotations__)
        if not name.startswith("_")
    }


@functools.cache
def get_stage(path: str) -> Stage:
    """
    Return the stage whose class is at the import path *path*, made with no arguments.

    Raises
    ------
    MissingClass
        If the class cannot be imported on this machine.
    ValueError
        If *path* is not an import path, or the class is not a stage: it
        does not subclass :class:`Stage` or lacks one of its required
        members, or its ``name``, ``suffix``, ``config_class``, ``result``
        or ``version`` cannot be used.
    """
    if "." not in path:
        raise ValueError(
            f"A stage is named by the import path of its class, such as "
            f"mypkg.stages.MyStage, not {path!r}."
        )
    cls = import_class(path)
    if not (isinstance(cls, type) and issubclass(cls, Stage)):
        raise ValueError(
            f"{path} is not a stage: its class must subclass runtraj.stages.Stage."
        )
    missing = [m for m in ("name", "config_class", "suffix") if not hasattr(cls, m)]
    missing += sorted(cls.__abstractmethods__)
    if missing:
        raise ValueError(f"{path} is not a stage: it has no {', '.join(missing)}.")
    stage = cls()
    name = stage.name
    if not isinstance(name, str) or not STAGE_NAME_RE.fullmatch(name):
        raise ValueError(
            f"{path}: a stage's name must match {STAGE_NAME_RE.pattern}, not {name!r}."
        )
    if name in RESERVED_NAMES:
        raise ValueError(
            f"{path}: a stage cannot be named {name!r}, which is a folder of "
            "the output directory or a column of project.status()."
        )
    suffix = stage.suffix
    if not isinstance(suffix, str) or not suffix.startswith(".") or len(suffix) < 2:
        raise ValueError(
            f"{path}: a stage's suffix must be a file extension such as "
            f"'.parquet', not {suffix!r}."
        )
    config_class = stage.config_class
    if not (isinstance(config_class, type) and issubclass(config_class, BaseModel)):
        raise ValueError(
            f"{path}: a stage's config_class must be a pydantic model, not "
            f"{config_class!r}."
        )
    if stage.result not in RESULT_TYPES:
        raise ValueError(
            f"{path}: a stage's result must be one of {sorted(RESULT_TYPES)}, "
            f"not {stage.result!r}."
        )
    version = stage.version
    if version is not None and not (
        isinstance(version, str) and STAGE_VERSION_RE.fullmatch(version)
    ):
        raise ValueError(
            f"{path}: a stage's version must be None or a string such as '2', "
            f"of {STAGE_VERSION_RE.pattern}, not {version!r}."
        )
    return stage


def ordered(stages: Mapping[str, Stage]) -> list[str]:
    """
    Return the names of *stages* in the order they run: each after the stages it reads.

    Stages that do not read one another keep the order they were given in.

    Raises
    ------
    ValueError
        If a stage reads one that is not among *stages*, or stages read
        each other in a circle.
    """
    for name, stage in stages.items():
        for read in stage.reads:
            if read not in stages:
                raise ValueError(
                    f"The stage {name!r} reads the stage {read!r}, which "
                    f"stages: does not have (it has {sorted(stages)})."
                )
    order: list[str] = []
    waiting = list(stages)
    while waiting:
        ready = [n for n in waiting if all(r in order for r in stages[n].reads)]
        if not ready:
            raise ValueError(
                f"The stages {sorted(waiting)} read each other in a circle, so "
                "none of them can run first."
            )
        order.append(ready[0])
        waiting.remove(ready[0])
    return order


def check_stage(stage: Stage, config: Any) -> None:
    """
    Check that *stage* keeps the promises the worker and the output directory rely on, for one *config*.

    Call it from the tests of a stage of your own, with a config as
    :meth:`Stage.resolve` returns it, once for each kind of setting the
    stage has. It checks that the record :meth:`Stage.settings` writes is
    plain data, and that :attr:`Stage.config_class` reads it back to a
    config with the same record: a stored settings folder is found that
    way, so a stage that breaks it finds none of its results. It also
    checks that no public name of the stage's class is spelled almost like
    a member of :class:`Stage`, as ``read`` is, which runtraj would never
    call.

    Raises
    ------
    AssertionError
        Saying which promise is broken.
    """
    assert isinstance(stage, Stage), (
        f"{type(stage).__name__} must subclass runtraj.stages.Stage"
    )
    miss = near_miss(type(stage), _stage_hooks())
    assert miss is None, miss
    assert STAGE_NAME_RE.fullmatch(stage.name), (
        f"name {stage.name!r} must match {STAGE_NAME_RE.pattern}"
    )
    assert stage.name not in RESERVED_NAMES, f"name {stage.name!r} is reserved"
    assert stage.suffix.startswith("."), (
        f"suffix {stage.suffix!r} must be a file extension, such as '.parquet'"
    )
    assert issubclass(stage.config_class, BaseModel), (
        "config_class must be a pydantic model"
    )
    record = canonical(stage.settings(config))
    try:
        json.dumps(record)
    except TypeError as error:
        raise AssertionError(
            f"settings() must return plain data (mappings, lists, strings, "
            f"numbers): {error}"
        ) from None
    read_back = stage.config_class.model_validate(record)
    again = canonical(stage.settings(read_back))
    assert again == record, (
        f"{type(stage).__name__}.config_class does not read back what settings() "
        f"wrote: {record} became {again}"
    )


__all__ = [
    "RESERVED_NAMES",
    "STAGE_NAME_RE",
    "STAGE_VERSION_RE",
    "Stage",
    "check_stage",
    "get_stage",
    "ordered",
]

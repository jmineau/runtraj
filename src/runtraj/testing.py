"""
Run a stage of your own end to end through runtraj, in your tests, with no transport model installed.

:func:`run_stage` makes a small project around your stage, whose transport
model (:class:`ReplayModel`) runs nothing and hands back particles you
give it, and runs the project as ``runtraj run`` would: the worker, the
settings folders, the temporary path your stage writes to, completion by
file, and skipping a stage whose file exists. A change in runtraj that
breaks your stage then fails your tests, not a user's run.

.. code-block:: python

   from runtraj import PointRelease
   from runtraj.testing import run_stage


   def test_my_stage_runs_in_a_project(tmp_path):
       release = PointRelease(
           time="2023-01-01 12:00", longitude=-111.85, latitude=40.77, altitude=5.0
       )
       sim = run_stage(
           "mypkg.stages.MyStage",
           my_particles(release),
           release,
           tmp_path,
           settings={"threshold": 3},
       )
       assert sim.path("my_stage").read_text() == "..."

:func:`runtraj.stages.check_stage` checks one config's settings record and
is quicker; :func:`run_stage` checks the stage in a project.

:func:`check_model` is for a transport model of your own: it checks the promises
runtraj relies on.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from pydantic import ConfigDict, Field, ValidationError

from runtraj._plugins import near_miss
from runtraj.exceptions import SimulationError
from runtraj.models import (
    RESERVED_SETTINGS,
    MetConfig,
    ModelInfo,
    ModelRun,
    TransportConfig,
    TransportModel,
)
from runtraj.models.run import _refused, _release_points, _run_many, _run_one
from runtraj.particles import read_particles, write_particles
from runtraj.project import Project
from runtraj.results import model_result, result_type
from runtraj.stages import check_stage, get_stage

if TYPE_CHECKING:
    from runtraj.releases import Release
    from runtraj.simulation import Simulation

#: The import path of :class:`ReplayModel`, the ``class`` of its block under ``models:``.
REPLAY_MODEL = "runtraj.testing.ReplayModel"


class ReplayConfig(TransportConfig):
    """The replay model's config: only the settings every transport model has (``n_hours``, ``seed``)."""


class ReplayMet(MetConfig):
    """
    The replay model's met: a folder of particle tables, one ``<release id>.parquet`` per release.

    Its settings name no file, so the particles' settings folder does not
    depend on where the tables are.
    """

    model_config = ConfigDict(extra="forbid")

    directory: Path = Field(
        ...,
        description="Folder of the particle tables, one <release id>.parquet each.",
    )

    def settings(self) -> dict[str, Any]:
        """Return what a run records of the met: only that it is a replay."""
        return {"replay": True}


class ReplayModel(TransportModel[ReplayConfig]):
    """
    A transport model that runs nothing: each release's particles are the table saved for it.

    For each release it reads ``<release id>.parquet`` from its met's
    ``directory`` and returns it as the run's particles, as a model that
    writes rows at ``age = 0`` would. It needs no workdir, reads no met
    files, and has no executable, so a project of it runs anywhere.
    """

    name = "replay"
    config_class = ReplayConfig
    met_config_class = ReplayMet
    needs_workdir = False

    def run(
        self,
        release: Release,
        config: ReplayConfig,
        met: ReplayMet,
        window: tuple[dt.datetime, dt.datetime],
        workdir: Path | None = None,
        timeout: int | None = None,
    ) -> ModelRun:
        """
        Return the particles saved for *release*.

        Raises
        ------
        FileNotFoundError
            If no table was saved for the release.
        """
        path = Path(met.directory) / f"{release.id}.parquet"
        return ModelRun(result=pd.read_parquet(path))


def run_stage(
    stage: str,
    particles: pd.DataFrame,
    release: Release,
    directory: str | Path,
    settings: Mapping[str, Any] | None = None,
    others: Mapping[str, Mapping[str, Any]] | None = None,
) -> Simulation:
    """
    Run the stage at the import path *stage* in a project of one release, and check what a project relies on.

    The project, in *directory* (an empty folder, such as pytest's
    ``tmp_path``), has one release, one variant, and :class:`ReplayModel`
    as its transport model, which hands back *particles* as the release's
    run. It is run as ``runtraj run`` runs it, in this process. It is for a
    stage that reads particles (:attr:`runtraj.stages.Stage.result`). Then
    this checks that:

    - every simulation is complete: the particles, and the file of each stage;
    - the stage's file is at :meth:`runtraj.Simulation.path`, where
      completion looks for it;
    - a second run makes nothing again;
    - :func:`runtraj.stages.check_stage` holds for the stage's settings as the
      variant resolved them.

    Parameters
    ----------
    stage : str
        The import path of the stage's class, as ``config.yaml`` names it.
    particles : pandas.DataFrame
        The particle table of *release*, with the columns your stage reads
        (:doc:`/reference/particles`). Its most negative or positive
        ``age`` sets ``n_hours``.
    release : Release
        The release the particles started from.
    directory : str or Path
        An empty folder for the project, its output, and the particles.
    settings : mapping, optional
        The stage's block under ``stages:``, without ``class``.
    others : mapping, optional
        The blocks of the stages it reads, by name, each with its ``class``.

    Returns
    -------
    Simulation
        The release's simulation, to read the stage's file with your own
        reader (``sim.path(<stage name>)``).

    Raises
    ------
    AssertionError
        Saying what a project relies on that the stage broke, with the
        reason and message of a simulation that failed.
    """
    directory = Path(directory)
    tables = directory / "particles"
    tables.mkdir(parents=True, exist_ok=True)
    particles.to_parquet(tables / f"{release.id}.parquet")
    name = get_stage(stage).name
    ages = pd.to_numeric(particles["age"])
    longest = ages.loc[ages.abs().idxmax()] / 60.0
    n_hours = int(math.copysign(max(1, math.ceil(abs(longest))), longest))
    project = Project.init(
        directory / "project",
        releases=[release],
        mets={"replay": {"directory": str(tables)}},
        models={"replay": {"class": REPLAY_MODEL, "n_hours": n_hours}},
        stages={**(others or {}), name: {"class": stage, **(settings or {})}},
        variants={"replay": {}},
    )
    compute = directory / "compute"
    project.run(compute_root=compute)
    status = project.status()
    unfinished = status[status["state"] != "complete"]
    if not unfinished.empty:
        row = unfinished.iloc[0]
        raise AssertionError(
            f"{stage} did not complete in a project: {row['state']}, "
            f"{row.get('reason')}: {row.get('message')}"
        )
    sim = project.simulation(str(release.id), "replay")
    path = sim.path(name)
    assert path is not None and sim.has(name), (
        f"{stage} wrote no file where completion looks for it ({path})"
    )
    stamp = path.stat().st_mtime_ns
    project.run(compute_root=compute)
    assert path.stat().st_mtime_ns == stamp, (
        f"{stage} ran again on a second run, though its file existed"
    )
    variant = project.variants["replay"]
    check_stage(variant.stage(name), variant.stages[name])
    return sim


def _model_hooks() -> set[str]:
    """Return the names of :class:`~runtraj.models.TransportModel`'s members: its hooks and attributes."""
    return {
        name
        for name in (*vars(TransportModel), *TransportModel.__annotations__)
        if not name.startswith("_")
    }


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

    - *model* subclasses :class:`~runtraj.models.TransportModel` and sets ``name``, and no
      public name of its class is spelled almost like a member of the base
      class, as ``release_row`` is, which runtraj would never call;
    - ``config_class`` is a :class:`~runtraj.models.TransportConfig` whose fields use none
      of :data:`~runtraj.models.RESERVED_SETTINGS`, and ``met_config_class`` a :class:`~runtraj.models.MetConfig`;
    - the record ``config.settings()`` writes is plain data, and
      ``config_class`` reads it back to a config with the same record: a
      stored settings folder is found that way; and so for each config
      ``config.realizations(3)`` returns, unless it refuses with a
      ``ValueError``;
    - ``sha256``, ``version`` and ``data_files`` make a :class:`~runtraj.models.ModelInfo`
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
    >>> from runtraj.testing import check_model
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
    """Check the model's build makes a :class:`~runtraj.models.ModelInfo`, as a variant builds it."""
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
    if str(release.id) not in _refused(model, [release], config):
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


__all__ = ["ReplayModel", "check_model", "run_stage"]

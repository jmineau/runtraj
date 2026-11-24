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
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from pydantic import ConfigDict, Field

from runtraj.models import MetConfig, ModelRun, TransportConfig, TransportModel
from runtraj.project import Project
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
    run. It is run as ``runtraj run`` runs it, in this process. Then this
    checks that:

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


__all__ = [
    "REPLAY_MODEL",
    "ReplayConfig",
    "ReplayMet",
    "ReplayModel",
    "run_stage",
]

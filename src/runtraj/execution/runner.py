"""
Running a project: finding the releases with missing results and handing them to workers.

What every backend shares: which releases run (:func:`pending`), the
share of a task (:func:`task_share`), the compute root, a local run in this
process, and the fetch before it. :func:`run` and :func:`submit` hand a
Slurm job array to :mod:`runtraj.execution.slurm` and a Kubernetes Job to
:mod:`runtraj.execution.kubernetes`.
"""

from __future__ import annotations

import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from runtraj._paths import absolute
from runtraj._transient import is_transient
from runtraj.execution.backends import Backend, get_backend, not_waited
from runtraj.execution.config import ExecutionConfig, LocalConfig, PbsConfig
from runtraj.execution.inputs import lock
from runtraj.fetch import downloads, fetch_project

if TYPE_CHECKING:
    import pandas as pd

    from runtraj.execution.config import SlurmConfig
    from runtraj.project import Project

logger = logging.getLogger(__name__)


#: Seconds :func:`run` waits to list the results once more when some look
#: unfinished after a job array ends. On a network filesystem the
#: submitting node can see a compute node's new files up to a minute late.
RELIST_SECONDS = 60


# ---------------------------------------------------------------------------
# Shares of the releases
# ---------------------------------------------------------------------------


def task_share(release_ids: list[str], task: int, n_tasks: int) -> list[str]:
    """
    Return task *task*'s share of *release_ids*, split *n_tasks* ways.

    The share is every *n_tasks*-th release, starting at *task*, which runs
    from 0 to ``n_tasks - 1`` as the index of a job array does.

    Raises
    ------
    ValueError
        If *task* is not from 0 to ``n_tasks - 1``.
    """
    if n_tasks < 1 or not 0 <= task < n_tasks:
        raise ValueError(
            f"A task runs from 0 to n_tasks - 1; got task {task} of {n_tasks}."
        )
    return release_ids[task::n_tasks]


# ---------------------------------------------------------------------------
# Running a project
# ---------------------------------------------------------------------------


def _project_slug(directory: str | Path) -> str:
    """Return a lowercase, hyphenated name for a project directory, for the Slurm job name."""
    name = Path(str(directory).rstrip("/")).name or "project"
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower().replace("_", "-"))
    return re.sub(r"-{2,}", "-", slug).strip("-") or "project"


def resolve_compute_root(
    project: Project, compute_root: str | Path | None = None
) -> Path:
    """
    Return the compute root of *project*: the directory its workdirs are made in.

    That is *compute_root* when given, else the ``RUNTRAJ_COMPUTE_ROOT``
    environment variable when it is set and not empty, else
    ``$TMPDIR/runtraj/<project name>``. The path is absolute and resolved, so
    a worker handed it gets the same directory. Projects can share one
    compute root, since each run gets a new workdir in it.
    """
    if compute_root is None:
        compute_root = os.environ.get("RUNTRAJ_COMPUTE_ROOT") or None
    if compute_root is not None:
        return absolute(compute_root)
    tmp_root = os.environ.get("TMPDIR") or tempfile.gettempdir()
    return absolute(Path(tmp_root) / "runtraj" / project.name)


def pending(
    project: Project,
    *,
    releases: Iterable[str] | None = None,
    task: tuple[int, int] | None = None,
    skip_existing: bool = True,
) -> list[str]:
    """
    Return the ids of the releases a run would run, each once, in project order.

    A driver that starts tasks itself writes these to the ``releases.txt``
    each task reads (``runtraj run --releases``).

    Parameters
    ----------
    project : Project
        The project.
    releases : iterable of str, optional
        Only these releases, in this order.
    task : tuple of (int, int), optional
        ``(i, n)``: share ``i`` of ``n`` (:func:`task_share`), taken from
        all the releases (or all of *releases*) before the complete ones
        are dropped, so the tasks of a job array split the releases the
        same way whenever each starts.
    skip_existing : bool, default True
        Leave out releases whose simulations are all complete.

    Raises
    ------
    ValueError
        If a release in *releases* is not in the project.
    """
    sims = project.simulations
    every = list(dict.fromkeys(sims["release"]))
    if releases is None:
        chosen = every
    else:
        chosen = list(dict.fromkeys(str(r) for r in releases))
        known = set(every)
        unknown = [r for r in chosen if r not in known]
        if unknown:
            raise ValueError(
                f"{len(unknown)} release ids are not in this project, such as "
                f"{unknown[:3]}."
            )
    if task is not None:
        chosen = task_share(chosen, *task)
    if skip_existing and chosen:
        left = set(project.incomplete(sims[sims["release"].isin(chosen)])["release"])
        chosen = [r for r in chosen if r in left]
    return chosen


def _to_fetch(
    project: Project,
    release_ids: list[str],
    execution: ExecutionConfig,
    skip_existing: bool,
) -> pd.DataFrame | None:
    """
    Return the simulations of *release_ids* whose met files are got once before they run, or ``None``.

    ``None`` when the backend's ``fetch`` is off, when no met of theirs
    downloads (:func:`runtraj.fetch.downloads`), or when none of them runs
    its transport model: those whose particles are missing, or every one
    when not *skip_existing*, since such a run makes the particles again.
    The output is listed only when a met downloads.
    """
    if execution.fetch is False:
        return None
    sims = project.simulations
    chosen = sims[sims["release"].isin(release_ids)]
    if not downloads(project, chosen["variant"].unique()):
        return None
    unrun = project._unrun(chosen, skip_existing=skip_existing)
    return None if unrun.empty else unrun


def _fetch_here(
    project: Project, unrun: pd.DataFrame, setting: str = "execution.local.fetch"
) -> None:
    """
    Get the met files of the *unrun* simulations in this process, before a local run's workers start.

    A file that could not be had, or a download that failed and that a
    retry may get past (:func:`runtraj._transient.is_transient`), is only
    logged: each run then tries again, and fails as it would have without
    the fetch. Any other error is raised.
    """
    logger.info("Getting the met files first (%s).", setting)
    try:
        files = fetch_project(project, unrun)
    except Exception as error:
        if not is_transient(error):
            raise
        logger.warning("The fetch stopped (%s); each run downloads its own.", error)
        return
    problems = files["message"].dropna()
    if len(problems):
        logger.warning(
            "%d met windows could not be had, such as: %s",
            len(problems),
            problems.iloc[0],
        )


#: The lock a task holds where the inputs are while it gets the met files (:func:`fetch_shared`).
FETCH_LOCK = "fetch"


def fetch_shared(project: Project, location: str) -> None:
    """
    Get the met files of every unfinished simulation, one task at a time, before this task's share runs.

    For the tasks of one Job, such as pods that share a met directory on a
    volume (``runtraj run --task I/N --fetch``, which ``runtraj submit``
    gives each pod with ``execution.kubernetes.fetch``). Each holds the
    lock :data:`FETCH_LOCK` at *location* while it fetches
    (:func:`runtraj.execution.inputs.lock`), so the first downloads the
    files and the others, waiting their turn, find them and download
    nothing. Nothing is locked when no met downloads. As for a local run's
    fetch, a file that could not be had, or a download a retry may get
    past, is only logged: each run then tries again.

    Parameters
    ----------
    project : Project
        The project, opened here.
    location : str
        Where every task reads the project's inputs: the URL they were
        copied to, or the project folder on a shared volume.

    Raises
    ------
    OSError
        If the lock cannot be made, such as a bucket that cannot be reached.
    """
    sims = project.simulations
    if not downloads(project, sims["variant"].unique()):
        return
    with lock(location, FETCH_LOCK):
        # Listed under the lock: what the task before got is not fetched again.
        unrun = project._unrun(sims, skip_existing=True)
        if not unrun.empty:
            _fetch_here(project, unrun, "execution.kubernetes.fetch")


def _status(project: Project, release_ids: list[str]) -> pd.DataFrame:
    """Return the status table of the simulations of *release_ids*."""
    sims = project.simulations
    return project.status(sims[sims["release"].isin(release_ids)])


def _unfinished(table: pd.DataFrame) -> bool:
    """Whether a status table has simulations that did not finish (pending or interrupted)."""
    return bool(table["state"].isin(["pending", "interrupted"]).any())


# ---------------------------------------------------------------------------
# What a scheduler's job array needs: its folder, its size, the listing after
# ---------------------------------------------------------------------------


def write_submission(
    project: Project, pending: list[str], execution: ExecutionConfig, scheduler: str
) -> Path:
    """
    Make a submission folder, ``_<scheduler>/<date_time>_<id>/`` in the project, and return it.

    It holds ``releases.txt``, the ids of *pending*; ``releases.parquet``,
    their checked rows in the order the tasks split them
    (:func:`task_share`), which each task reads in place of
    ``releases.csv``; and ``execution.yaml``, the execution settings. One
    folder per submission, so a later one never overwrites these.
    """
    import pyarrow as pa
    import yaml

    from runtraj._paths import write_parquet

    stamp = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:6]}"
    folder = Path(project.directory) / f"_{scheduler}" / stamp
    folder.mkdir(parents=True)
    (folder / "releases.txt").write_text("\n".join(pending) + "\n", encoding="utf-8")
    rows = project._rows_of(pending)
    write_parquet(
        pa.Table.from_pandas(rows, preserve_index=False), folder / "releases.parquet"
    )
    (folder / "execution.yaml").write_text(
        yaml.safe_dump(execution.model_dump(exclude_unset=True), sort_keys=False),
        encoding="utf-8",
    )
    return folder


def array_size(block: SlurmConfig | PbsConfig, n_releases: int, limit: int) -> int:
    """
    Return the number of tasks of a job array for *n_releases*: ``n_tasks``, or ten per worker.

    *block* is the ``slurm`` or ``pbs`` block. Never more than one task
    per release, nor more than *limit*, the most tasks the cluster allows
    in one array (Slurm's ``MaxArraySize``, PBS's ``max_array_size``). A
    task runs ``cpus`` releases at once, so with ``n_tasks`` unset a task's
    share is whole rounds of ``cpus`` releases: a share of 10 on 8 CPUs
    would leave 6 of them idle for its last 2 releases.

    Examples
    --------
    >>> from runtraj.execution import SlurmConfig
    >>> array_size(SlurmConfig(n_workers=50), 10_000, limit=1000)
    500
    >>> array_size(SlurmConfig(n_workers=50, cpus=8), 10_000, limit=1000)
    417
    >>> array_size(SlurmConfig(n_workers=200), 10_000, limit=1000)
    1000
    >>> array_size(SlurmConfig(n_workers=50), 120, limit=1000)
    120

    Raises
    ------
    ValueError
        If ``n_tasks`` is more than *limit*.
    """
    if block.n_tasks is not None and block.n_tasks > limit:
        name = "pbs" if isinstance(block, PbsConfig) else "slurm"
        raise ValueError(
            f"execution.{name}.n_tasks is {block.n_tasks}, and this cluster's job "
            f"arrays have at most {limit} tasks. Set n_tasks to {limit} or "
            "fewer, or leave it unset."
        )
    tasks = max(1, min(block.tasks, n_releases, limit))
    if block.n_tasks is None and block.cpus > 1:
        share = math.ceil(n_releases / tasks / block.cpus) * block.cpus
        tasks = max(1, math.ceil(n_releases / share))
    return tasks


def status_after_job(project: Project, release_ids: list[str]) -> pd.DataFrame:
    """
    Return the status table of *release_ids* after a job array ended.

    When some simulations look unfinished, their results are listed once
    more :data:`RELIST_SECONDS` later, since a network filesystem can show
    the submitting node a compute node's new files late.
    """
    table = _status(project, release_ids)
    if _unfinished(table):
        logger.info(
            "Some simulations look unfinished; listing again in %d s, in "
            "case this node sees the compute nodes' files late.",
            RELIST_SECONDS,
        )
        time.sleep(RELIST_SECONDS)
        table = _status(project, release_ids)
    return table


def run(
    project: Project,
    *,
    releases: Iterable[str] | None = None,
    task: tuple[int, int] | None = None,
    execution: ExecutionConfig | None = None,
    skip_existing: bool = True,
    compute_root: str | Path | None = None,
) -> pd.DataFrame:
    """
    Run every simulation of a project that has not finished, and wait for it.

    Each release with missing results runs once: the transport model once for each
    distinct set of transport settings whose particles are missing, then the
    stages of every variant that has some. With ``backend: local`` the
    releases run in this process (``cpus`` at a time); with ``cpus`` above
    1 and a met that downloads, the met files are got first
    (:meth:`runtraj.Project.fetch`, unless ``execution.local.fetch`` is ``false``),
    so the workers do not download the same file at once. With
    ``backend: slurm`` they are submitted as one job array (:func:`submit`)
    and this waits for it. When some simulations look unfinished after the
    job ends, their results are listed once more :data:`RELIST_SECONDS`
    later, since a network filesystem can show this node new files late.
    Another backend runs them as its :meth:`~runtraj.execution.Backend.run`
    does, and one that runtraj does not wait for (``kubernetes``) is refused.

    Parameters
    ----------
    project : Project
        Project to run.
    releases : iterable of str, optional
        Run only these releases (their ids), in this order.
    task : tuple of (int, int), optional
        ``(i, n)``: run share ``i`` of ``n`` of the releases here, in this
        process, whatever the backend. The share is every ``n``-th
        release in project order (or in *releases* order), starting at
        ``i`` (:func:`task_share`), whether it is complete or not, and the
        complete ones are then skipped. Each task of a job array or a
        Kubernetes indexed Job runs one share, and every task splits the
        releases the same way whenever it starts.
    execution : ExecutionConfig, optional
        Where to run and with what resources. Defaults to the ``execution``
        settings of the project's config.
    skip_existing : bool, default True
        Skip simulations whose results all exist. ``False`` runs every
        simulation again.
    compute_root : str or Path, optional
        Directory the workdirs are made in, a new one for each run
        (:func:`resolve_compute_root`). On Slurm it is resolved on the
        compute node unless given here.

    Returns
    -------
    pandas.DataFrame
        The status table (:meth:`runtraj.Project.status`) of the
        simulations that ran: ``state`` says which are complete, which
        failed and why, and which did not finish.

    Raises
    ------
    ValueError
        If a release id is not in the project, *task* is not from 0 to
        ``n - 1``, or the backend is not waited for.

    Notes
    -----
    A task (``task`` given) stopped with SIGTERM or Ctrl-C raises
    ``KeyboardInterrupt``, and ``runtraj run`` exits with 3. Whoever
    started the task decides whether it runs again: the Slurm job array
    script requeues it after the time-limit notice or a preemption
    (:func:`runtraj.execution.job_script`), and a Kubernetes Job retries it
    (:mod:`runtraj.execution.kubernetes`). It then skips what it finished.
    """
    execution = execution if execution is not None else project.config.execution
    backend = get_backend(execution.backend)
    if task is None and not backend.waits:
        raise ValueError(not_waited(backend))
    todo = pending(project, releases=releases, task=task, skip_existing=skip_existing)
    if not todo:
        logger.info("run: every simulation is complete; nothing to do")
        return _status(project, todo)
    if task is not None:
        # One task of a job array, in this process, whatever the backend. A
        # task stopped with work left exits with 3, and its scheduler starts
        # it again (the Slurm job script, a Kubernetes pod failure policy).
        run_here(project, todo, execution, skip_existing, compute_root)
        return _status(project, todo)
    return backend.run(
        project,
        todo,
        execution,
        skip_existing=skip_existing,
        compute_root=compute_root,
    )


def run_here(
    project: Project,
    release_ids: list[str],
    execution: ExecutionConfig,
    skip_existing: bool,
    compute_root: str | Path | None,
) -> None:
    """Run *release_ids* in this process, ``cpus`` at a time, so Ctrl-C and SIGTERM stop the workers and progress prints as it happens."""
    from .worker import run_releases

    logger.info("run(%s): %d releases", ", ".join(project.variants), len(release_ids))
    run_releases(
        project,
        release_ids,
        compute_root=resolve_compute_root(project, compute_root),
        execution=execution,
        skip_existing=skip_existing,
    )


#: Why :func:`submit` refuses the ``local`` backend.
NOTHING_TO_SUBMIT = (
    "submit sends work to a scheduler, and this run's backend is 'local'. "
    "Use run(), or set execution.backend to slurm or kubernetes."
)


class LocalBackend(Backend):
    """
    The ``local`` backend: the releases run here, in this process, ``cpus`` at a time.

    With ``cpus`` above 1 and a met that downloads, the met files are got
    first, unless ``execution.local.fetch`` is ``false``, so the workers do
    not download the same file at once. There is nothing to submit.
    """

    name = "local"
    config_class = LocalConfig
    waits = True
    here = True

    def submit(
        self,
        project: Project,
        releases: list[str],
        execution: ExecutionConfig,
        *,
        skip_existing: bool,
        compute_root: str | Path | None,
    ) -> str:
        """
        Raise: a local run has nothing to submit.

        Raises
        ------
        ValueError
            Always.
        """
        raise ValueError(NOTHING_TO_SUBMIT)

    def run(
        self,
        project: Project,
        releases: list[str],
        execution: ExecutionConfig,
        *,
        skip_existing: bool,
        compute_root: str | Path | None,
    ) -> pd.DataFrame:
        """Get the met files first when the workers would download them at once, run *releases* here, and return their status table."""
        unrun = (
            _to_fetch(project, releases, execution, skip_existing)
            if execution.cpus > 1
            else None
        )
        if unrun is not None:
            _fetch_here(project, unrun)
        run_here(project, releases, execution, skip_existing, compute_root)
        return _status(project, releases)

    def describe(self, waits: bool) -> str:
        """Return the line a local run prints before it starts."""
        return "Running here, one line per release as it finishes"


def submit(
    project: Project,
    *,
    releases: Iterable[str] | None = None,
    execution: ExecutionConfig | None = None,
    skip_existing: bool = True,
    compute_root: str | Path | None = None,
) -> str | None:
    """
    Submit every simulation of a project that has not finished to Slurm, Kubernetes or another backend.

    The releases with missing results are split among the ``n_tasks``
    tasks of one job array (:func:`runtraj.execution.slurm.array_size`), at
    most ``n_workers`` running at once, and this returns once it is
    submitted. The
    submission is a folder ``_slurm/<date_time>_<id>/`` in the project:
    ``releases.txt`` lists the releases to run, ``releases.parquet``
    holds their checked rows from ``releases.csv``, ``execution.yaml``
    holds the execution settings, ``job.sh`` is the script given to
    ``sbatch``, and ``<task>.log`` is each task's log. Task ``i`` runs
    ``runtraj run <project> --releases releases.parquet --task i/N``, so it
    does not read and check all of ``releases.csv`` again. A task
    that is preempted or nears its time limit requeues itself and skips
    what it finished (see :func:`run`).

    When a met the unfinished simulations use downloads its files, a
    fetch job goes first: one task that runs ``runtraj fetch <project>
    --releases releases.parquet`` (:func:`runtraj.execution.slurm.fetch_script`), written to
    ``fetch.sh`` with its log in ``fetch.log``, with the resources of
    ``execution.slurm.fetch``. The array waits for it to end
    (``--dependency=afterany``), however it ended, so the runs whose met it
    got go ahead, and each task downloads what is still missing. With
    ``skip_existing=False`` it gets the met of every run the array makes
    again. If ``sbatch`` refuses the array, the fetch job is cancelled.
    Set ``execution.slurm.fetch`` to ``false`` for no fetch job.

    With ``backend: kubernetes`` it makes one indexed Job instead, whose
    pods each run ``runtraj run <project> --task I/N``
    (:func:`runtraj.execution.kubernetes.submit_job`), and creates it with
    ``kubectl``. Each task runs its share of every release of the project
    and skips the complete ones, so *releases* and ``skip_existing=False``
    are refused there, and *compute_root* is each pod's own. Another
    backend starts its tasks as its :meth:`~runtraj.execution.Backend.submit`
    does.

    Parameters
    ----------
    project, releases, execution, skip_existing, compute_root
        As for :func:`run`.

    Returns
    -------
    str or None
        The Slurm job id of the array, or the name of the Kubernetes Job,
        or ``None`` when nothing needs to run. ``squeue -j <id>`` follows a
        job array and ``scancel <id>`` stops it; ``kubectl get job <name>``
        and ``kubectl delete job <name>`` do the same for a Job. A fetch
        job is a job of its own: its id is logged and written to
        ``fetch.id`` in the submission folder, and ``scancel`` stops it too.

    Raises
    ------
    ValueError
        If the execution backend is ``local``, or one that takes no list
        of releases (Kubernetes) with *releases* or ``skip_existing=False``.
    """
    execution = execution if execution is not None else project.config.execution
    backend = get_backend(execution.backend)
    if backend.here:
        raise ValueError(NOTHING_TO_SUBMIT)
    if not backend.takes_releases and (releases is not None or not skip_existing):
        raise ValueError(
            f"A {backend.name} job runs every unfinished release of the project "
            "and skips the complete ones, as a retried task must: it takes no "
            "list of releases and no --no-skip."
        )
    todo = pending(project, releases=releases, skip_existing=skip_existing)
    if not todo:
        logger.info("submit: every simulation is complete; nothing to do")
        return None
    return backend.submit(
        project,
        todo,
        execution,
        skip_existing=skip_existing,
        compute_root=compute_root,
    )


__all__ = [
    "FETCH_LOCK",
    "LocalBackend",
    "array_size",
    "NOTHING_TO_SUBMIT",
    "RELIST_SECONDS",
    "fetch_shared",
    "resolve_compute_root",
    "run",
    "run_here",
    "status_after_job",
    "submit",
    "task_share",
    "write_submission",
]

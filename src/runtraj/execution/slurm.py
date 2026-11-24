"""
The Slurm backend: a project's unfinished releases as a job array, and a task that Slurm stops.

With ``backend: slurm`` (:class:`SlurmBackend`), :func:`runtraj.execution.submit` writes a job array
script into ``_slurm/<stamp>/`` in the project and submits it with
``sbatch`` (:func:`submit_array`). Each task of the array runs ``runtraj
run --task``, so a task is a command line that can be read, and run again,
by hand. When a met downloads, a job that runs ``runtraj fetch`` goes first
and the array waits for it. :func:`wait` follows the tasks with ``sacct``
until they end, and a task that Slurm stops near its time limit or
preempts requeues itself (:func:`run_task`).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pyarrow as pa
import yaml

from runtraj._paths import write_parquet
from runtraj.execution.backends import Backend
from runtraj.execution.config import ExecutionConfig, FetchConfig, SlurmConfig
from runtraj.execution.runner import (
    RELIST_SECONDS,
    _project_slug,
    _status,
    _to_fetch,
    _unfinished,
    resolve_compute_root,
)

if TYPE_CHECKING:
    import pandas as pd

    from runtraj.project import Project

logger = logging.getLogger(__name__)


#: Seconds before a Slurm task's time limit that Slurm sends it SIGUSR1,
#: which stops the task and requeues it (``--signal=B:USR1@120``).
NOTICE_SECONDS = 120

#: Times a Slurm task requeues itself before it stops for good.
MAX_REQUEUES = 10


#: Slurm job states of a task that has not ended.
_ACTIVE = frozenset(
    {
        "PENDING",
        "RUNNING",
        "REQUEUED",
        "REQUEUE_HOLD",
        "REQUEUE_FED",
        "RESIZING",
        "SUSPENDED",
        "CONFIGURING",
        "COMPLETING",
        "SIGNALING",
        "STAGE_OUT",
    }
)


def _sbatch_lines(options: dict[str, Any]) -> list[str]:
    """Return ``#SBATCH`` lines; ``True`` is a bare flag, and ``None`` and ``False`` are left out."""
    lines = []
    for key, value in options.items():
        if value is None or value is False:
            continue
        flag = "--" + str(key).replace("_", "-")
        lines.append(f"#SBATCH {flag}" if value is True else f"#SBATCH {flag}={value}")
    return lines


def _threads_per_worker(options: dict[str, Any], execution: ExecutionConfig) -> int:
    """
    Return the threads each worker process gets: the task's CPUs shared among its workers.

    The task's CPUs are ``--cpus-per-task`` as written, so an ``sbatch``
    option that replaces it counts. With ``cpus`` workers on ``cpus`` CPUs
    this is 1.
    """
    try:
        task_cpus = int(options["cpus-per-task"])
    except (TypeError, ValueError):
        return 1  # sbatch will refuse the value; do not fail here
    return max(1, task_cpus // execution.slurm.cpus)


def job_script(
    project: Project,
    execution: ExecutionConfig,
    folder: Path,
    n_tasks: int,
    *,
    skip_existing: bool = True,
    compute_root: str | Path | None = None,
) -> str:
    """
    Return the ``sbatch`` script of a job array whose tasks each run a share of the releases.

    Task ``i`` runs ``runtraj run <project> --releases <folder>/releases.parquet
    --task i/n_tasks --execution <folder>/execution.yaml --cpus <cpus>`` with
    the Python that called this: a task runs its share here, ``cpus``
    releases at once. The ``#SBATCH`` lines come from *execution*'s
    ``slurm`` block: its resources, ``--requeue``, ``--signal=B:USR1@120``
    (see :func:`run`), and its other ``sbatch`` options, which can replace
    any of them. Its ``setup`` commands run before the task, after the
    script exports ``OMP_NUM_THREADS`` as the task's CPUs divided by the
    workers it runs at once (``cpus``), which a ``setup`` line can export
    again. NumPy's BLAS follows it, so a worker does not start a thread pool
    the size of the node.
    """
    slurm = execution.slurm
    options: dict[str, Any] = {
        "job-name": f"runtraj-{_project_slug(project.directory)}",
        "array": f"0-{n_tasks - 1}"
        + (f"%{slurm.array_parallelism}" if slurm.array_parallelism else ""),
        "cpus-per-task": slurm.cpus,
        "time": slurm.time,
        "mem": slurm.mem,
        "partition": slurm.partition,
        "account": slurm.account,
        "qos": slurm.qos,
        "output": folder / "%a.log",
        # A requeued task writes on at the end of its log.
        "open-mode": "append",
        "requeue": True,
        "signal": f"B:USR1@{NOTICE_SECONDS}",
    }
    for key, value in slurm.options.items():
        options[str(key).replace("_", "-")] = value

    command = [
        sys.executable,
        "-m",
        "runtraj",
        "run",
        str(project.directory),
        "--releases",
        str(folder / "releases.parquet"),
        "--task",
        f"$SLURM_ARRAY_TASK_ID/{n_tasks}",
        "--execution",
        str(folder / "execution.yaml"),
        # A task runs its share here, so it is told the array's cpus.
        "--cpus",
        str(slurm.cpus),
        # The output the submitting process uses, which may not be config.yaml's.
        "--output",
        str(project.output.directory),
    ]
    if compute_root is not None:
        command += ["--compute-root", str(resolve_compute_root(project, compute_root))]
    # $SLURM_ARRAY_TASK_ID must expand, so that word is quoted with "".
    words = [f'"{w}"' if w.startswith("$") else shlex.quote(w) for w in command]
    # A task's log starts with where and when it ran, again after a requeue,
    # so a task on a bad node can be found from its log.
    header = (
        'echo "$(date -u +%FT%TZ) $SLURMD_NODENAME '
        "job ${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID} "
        'restart ${SLURM_RESTART_COUNT:-0}"'
    )
    # One thread per worker unless the task has CPUs to spare. Before setup,
    # so an export there decides.
    threads = f"export OMP_NUM_THREADS={_threads_per_worker(options, execution)}"
    lines = [
        "#!/bin/bash",
        *_sbatch_lines(options),
        "",
        header,
        threads,
        *slurm.setup,
    ]
    if not skip_existing:
        # A requeued task keeps what it finished before.
        lines += [
            'skip="--no-skip"',
            '[ "${SLURM_RESTART_COUNT:-0}" -gt 0 ] && skip=""',
        ]
        words.append("$skip")
    # exec, so this process is the one Slurm signals.
    lines.append("exec " + " ".join(words))
    return "\n".join(lines) + "\n"


def fetch_script(
    project: Project,
    execution: ExecutionConfig,
    folder: Path,
    *,
    skip_existing: bool = True,
) -> str:
    """
    Return the ``sbatch`` script of the job that gets the met files before a job array.

    It runs ``runtraj fetch <project> --releases <folder>/releases.parquet``
    (with ``--no-skip`` when not *skip_existing*) with the Python that
    called this, as one task, after the ``setup`` commands. Its
    ``partition``, ``account``, ``qos``, ``time`` and ``mem`` are those of
    ``fetch`` in *execution*'s ``slurm`` block, each unset one the array's.
    Of the other ``sbatch`` options it takes only ``clusters``, so it runs
    on the cluster of the array that waits for it. Its log is
    ``<folder>/fetch.log``.
    """
    slurm = execution.slurm
    fetch = slurm.fetch or FetchConfig()
    options: dict[str, Any] = {
        "job-name": f"runtraj-fetch-{_project_slug(project.directory)}",
        "time": fetch.time or slurm.time,
        "mem": fetch.mem or slurm.mem,
        "partition": fetch.partition or slurm.partition,
        "account": fetch.account or slurm.account,
        "qos": fetch.qos or slurm.qos,
        "clusters": slurm.options.get("clusters"),
        "output": folder / "fetch.log",
    }
    command = [
        sys.executable,
        "-m",
        "runtraj",
        "fetch",
        str(project.directory),
        "--releases",
        str(folder / "releases.parquet"),
        "--output",
        str(project.output.directory),
    ]
    if not skip_existing:
        command.append("--no-skip")
    header = 'echo "$(date -u +%FT%TZ) $SLURMD_NODENAME job ${SLURM_JOB_ID}"'
    lines = [
        "#!/bin/bash",
        *_sbatch_lines(options),
        "",
        header,
        *slurm.setup,
        "exec " + " ".join(shlex.quote(w) for w in command),
    ]
    return "\n".join(lines) + "\n"


def _sbatch(script: Path, *extra: str) -> tuple[str, str | None]:
    """Submit *script* with ``sbatch --parsable`` and return its job id and cluster (``None`` for the default one)."""
    result = subprocess.run(
        ["sbatch", "--parsable", *extra, str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"sbatch refused {script}: {result.stderr.strip() or result.stdout.strip()}"
        )
    # --parsable prints "<id>", or "<id>;<cluster>" for a job on another cluster.
    job_id, _, cluster = result.stdout.strip().partition(";")
    return job_id, cluster or None


def submit_array(
    project: Project,
    pending: list[str],
    execution: ExecutionConfig,
    skip_existing: bool,
    compute_root: str | Path | None,
) -> tuple[str, str | None]:
    """
    Write a submission folder for *pending* releases, submit its job array, and return its job id and cluster.

    The cluster is the one ``sbatch`` sent the job to when it is not the
    default (``clusters`` in the Slurm options), else ``None``. When the
    met files are got first (:func:`runtraj.execution.runner._to_fetch`), the fetch job is
    submitted before the array, which waits for it, and cancelled if
    ``sbatch`` refuses the array.
    """
    # One folder per submission, so a later array never overwrites these.
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:6]}"
    folder = project.directory / "_slurm" / stamp
    folder.mkdir(parents=True)
    (folder / "releases.txt").write_text("\n".join(pending) + "\n", encoding="utf-8")
    # The checked rows, in the order the tasks split them (task_share).
    rows = project._rows_of(pending)
    write_parquet(
        pa.Table.from_pandas(rows, preserve_index=False), folder / "releases.parquet"
    )
    (folder / "execution.yaml").write_text(
        yaml.safe_dump(execution.model_dump(exclude_unset=True), sort_keys=False),
        encoding="utf-8",
    )
    n_tasks = max(1, min(execution.slurm.n_workers, len(pending)))
    script = folder / "job.sh"
    script.write_text(
        job_script(
            project,
            execution,
            folder,
            n_tasks,
            skip_existing=skip_existing,
            compute_root=compute_root,
        ),
        encoding="utf-8",
    )
    after: list[str] = []
    fetch_id, fetch_cluster = None, None
    if _to_fetch(project, pending, execution, skip_existing) is not None:
        fetch = folder / "fetch.sh"
        fetch.write_text(
            fetch_script(project, execution, folder, skip_existing=skip_existing),
            encoding="utf-8",
        )
        fetch_id, fetch_cluster = _sbatch(fetch)
        (folder / "fetch.id").write_text(fetch_id + "\n", encoding="utf-8")
        logger.info(
            "Submitted fetch job: %s%s; the job array waits for it to end. Log in %s",
            fetch_id,
            f" on cluster {fetch_cluster}" if fetch_cluster else "",
            folder / "fetch.log",
        )
        # afterany: a fetch that lacked some files (exit 1), was stopped, or
        # could not reach the archive still lets the runs whose met is there go.
        after = [f"--dependency=afterany:{fetch_id}"]
        if fetch_cluster:
            # The array must be on the fetch job's cluster to wait for it.
            after.append(f"--clusters={fetch_cluster}")
    try:
        job_id, cluster = _sbatch(script, *after)
    except RuntimeError:
        if fetch_id is not None:
            # Nothing waits for the fetch job now: do not leave it downloading.
            subprocess.run(
                ["scancel", *_on_cluster(fetch_cluster), fetch_id],
                capture_output=True,
                check=False,
            )
            logger.warning(
                "Cancelled fetch job %s: the job array was refused.", fetch_id
            )
        raise
    logger.info(
        "Submitted job: %s%s (%d tasks); logs in %s",
        job_id,
        f" on cluster {cluster}" if cluster else "",
        n_tasks,
        folder,
    )
    return job_id, cluster


def _on_cluster(cluster: str | None) -> list[str]:
    """Return the words that send a Slurm command to *cluster* (``-M``); none for the default one."""
    return ["-M", cluster] if cluster else []


def _task_states(
    job_id: str, cluster: str | None = None
) -> dict[str, tuple[str, str]] | None:
    """
    Return ``{task: (state, exit code)}`` for the tasks of a job array, from ``sacct``.

    ``None`` when ``sacct`` cannot answer, as when the accounting database
    is slow to hear of a new job.
    """
    result = subprocess.run(
        [
            "sacct",
            *_on_cluster(cluster),
            "-j",
            job_id,
            "-X",
            "-n",
            "-P",
            "--format=JobID,State,ExitCode",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    states = {}
    for line in result.stdout.splitlines():
        parts = line.split("|")
        if len(parts) == 3:
            task, state, code = parts
            states[task] = (state.split()[0] if state else "", code)
    return states or None


def wait(
    job_id: str, poll: float = 30.0, *, cluster: str | None = None
) -> dict[str, tuple[str, str]]:
    """
    Wait until every task of a Slurm job array has ended, and return how each ended.

    Polls ``sacct`` every *poll* seconds. A task that ended other than
    complete, or with failed simulations (exit 1) or interrupted ones
    (exit 3), is logged with its state; its simulations stay pending in the
    status table.

    Parameters
    ----------
    job_id : str
        The job array's id.
    poll : float, default 30
        Seconds between ``sacct`` calls.
    cluster : str, optional
        The Slurm cluster the job is on, when it is not the default one
        (``clusters`` in the Slurm options).

    Returns
    -------
    dict
        ``{task: (state, exit code)}`` as ``sacct`` gives them, such as
        ``{"123_0": ("COMPLETED", "0:0")}``.
    """
    states = _task_states(job_id, cluster)
    while states is None or any(s in _ACTIVE for s, _ in states.values()):
        time.sleep(poll)
        states = _task_states(job_id, cluster)
    for task, (state, code) in sorted(states.items()):
        if state == "COMPLETED" or (state == "FAILED" and code in ("1:0", "3:0")):
            continue
        logger.warning(
            "Slurm task %s ended %s (exit %s); see its log", task, state, code
        )
    return states


class SlurmBackend(Backend):
    """
    The ``slurm`` backend: the releases as the tasks of one Slurm job array (:func:`submit_array`).

    ``runtraj run`` waits for the array with ``sacct`` (:func:`wait`), and
    when some simulations look unfinished after it ends, lists their
    results once more :data:`runtraj.execution.runner.RELIST_SECONDS`
    later, since a network filesystem can show this node new files late.
    """

    name = "slurm"
    config_class = SlurmConfig
    waits = True

    def submit(
        self,
        project: Project,
        releases: list[str],
        execution: ExecutionConfig,
        *,
        skip_existing: bool,
        compute_root: str | Path | None,
    ) -> str:
        """Submit the job array, after its fetch job when a met downloads, and return its job id."""
        job_id, _ = submit_array(
            project, releases, execution, skip_existing, compute_root
        )
        return job_id

    def run(
        self,
        project: Project,
        releases: list[str],
        execution: ExecutionConfig,
        *,
        skip_existing: bool,
        compute_root: str | Path | None,
    ) -> pd.DataFrame:
        """Submit the job array, wait for every task to end, and return the status table of *releases*."""
        job_id, cluster = submit_array(
            project, releases, execution, skip_existing, compute_root
        )
        wait(job_id, cluster=cluster)
        table = _status(project, releases)
        if _unfinished(table):
            logger.info(
                "Some simulations look unfinished; listing again in %d s, in "
                "case this node sees the compute nodes' files late.",
                RELIST_SECONDS,
            )
            time.sleep(RELIST_SECONDS)
            table = _status(project, releases)
        return table

    def describe(self, waits: bool) -> str:
        """Return the line ``runtraj run`` or ``runtraj submit`` prints before it submits."""
        if waits:
            return "Submitting to Slurm and waiting for the job to finish (squeue shows its tasks)"
        return "Submitting to Slurm and returning"


# ---------------------------------------------------------------------------
# A task that is told to stop
# ---------------------------------------------------------------------------


class _Notice:
    """
    Whether SIGUSR1 has arrived.

    While ``armed``, its first arrival raises ``KeyboardInterrupt`` to stop
    the work. Later ones, and any after the work, are only recorded, so
    they cannot cut a cleanup short or end the process.
    """

    def __init__(self) -> None:
        self.received = False
        self.armed = True

    def __bool__(self) -> bool:
        return self.received

    def handle(self, signum: int, frame: object) -> None:
        first = not self.received
        self.received = True
        if first and self.armed:
            raise KeyboardInterrupt


@contextlib.contextmanager
def _usr1_noticed() -> Iterator[_Notice]:
    """Record SIGUSR1 inside the block (:class:`_Notice`); outside the main thread, where handlers cannot be set, or on Windows, which has no SIGUSR1, nothing is."""
    notice = _Notice()
    # Windows has no SIGUSR1, and no Slurm to send it.
    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is None or threading.current_thread() is not threading.main_thread():
        yield notice
        return
    previous = signal.signal(sigusr1, notice.handle)
    try:
        yield notice
    finally:
        signal.signal(sigusr1, previous)


def run_task(
    project: Project, pending: list[str], work: Callable[[], None]
) -> pd.DataFrame:
    """
    Run a task's releases and return their status table; requeue the task when Slurm stopped it.

    A task that stops with simulations left is requeued when it got
    SIGUSR1 (the time limit is near) or was preempted.
    """
    with _usr1_noticed() as notice:
        try:
            work()
        except KeyboardInterrupt:
            if not notice:
                raise
        finally:
            notice.armed = False
        table = _status(project, pending)
        if _unfinished(table) and (notice or _preempted()):
            _requeue()
    return table


def _this_cluster() -> list[str]:
    """Return the words that send a Slurm command to the cluster this task runs on."""
    return _on_cluster(os.environ.get("SLURM_CLUSTER_NAME"))


def _preempted() -> bool:
    """Whether Slurm has selected this job for preemption (``scontrol show job`` gives a ``PreemptTime``)."""
    job_id = os.environ.get("SLURM_JOB_ID")
    if not job_id:
        return False
    result = subprocess.run(
        ["scontrol", *_this_cluster(), "show", "job", job_id],
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"PreemptTime=(\S+)", result.stdout)
    return match is not None and match.group(1) not in ("None", "Unknown")


def _requeue() -> bool:
    """Put this Slurm task back in the queue with ``scontrol requeue``; return whether it was."""
    job_id = os.environ.get("SLURM_JOB_ID")
    if not job_id:
        logger.warning("Told to stop outside a Slurm job; not requeued.")
        return False
    restarts = int(os.environ.get("SLURM_RESTART_COUNT") or 0)
    if restarts >= MAX_REQUEUES:
        logger.warning(
            "Slurm task %s has been requeued %d times; not again. Run the "
            "project again, or raise execution.slurm.time.",
            job_id,
            restarts,
        )
        return False
    result = subprocess.run(
        ["scontrol", *_this_cluster(), "requeue", job_id],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("scontrol requeue %s failed: %s", job_id, result.stderr.strip())
        return False
    logger.info(
        "Requeued Slurm task %s; it starts again and skips what finished.", job_id
    )
    return True


__all__ = [
    "MAX_REQUEUES",
    "NOTICE_SECONDS",
    "SlurmBackend",
    "fetch_script",
    "job_script",
    "run_task",
    "submit_array",
    "wait",
]

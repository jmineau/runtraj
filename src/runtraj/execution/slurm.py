"""
The Slurm backend: a project's unfinished releases as a job array, and a task that Slurm stops.

With ``backend: slurm`` (:class:`SlurmBackend`), :func:`runtraj.execution.submit` writes a job array
script into ``_slurm/<stamp>/`` in the project and submits it with
``sbatch`` (:func:`submit_array`). Each task of the array runs ``runtraj
run --task``, so a task is a command line that can be read, and run again,
by hand. When a met downloads, a job that runs ``runtraj fetch`` goes first
and the array waits for it. :func:`wait` follows the tasks with ``sacct``
until they end. A task that Slurm stops near its time limit, or preempts,
is requeued by its job script, which runs ``runtraj run`` as a child: the
worker itself knows nothing of Slurm, and stops with exit code 3 on
SIGTERM as it does on every backend.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from runtraj.execution.backends import Backend
from runtraj.execution.config import ExecutionConfig, FetchConfig, SlurmConfig
from runtraj.execution.runner import (
    _project_slug,
    _to_fetch,
    array_size,
    resolve_compute_root,
    status_after_job,
    write_submission,
)

if TYPE_CHECKING:
    import pandas as pd

    from runtraj.project import Project

logger = logging.getLogger(__name__)


#: Seconds before a Slurm task's time limit that Slurm sends its job script
#: SIGUSR1, which stops the task and requeues it (``--signal=B:USR1@120``).
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
    ``slurm`` block: its resources, ``--requeue``, ``--signal=B:USR1@120``,
    and its other ``sbatch`` options, which can replace any of them. Its
    ``setup`` commands run before the task, after the
    script exports ``OMP_NUM_THREADS`` as the task's CPUs divided by the
    workers it runs at once (``cpus``), which a ``setup`` line can export
    again. NumPy's BLAS follows it, so a worker does not start a thread pool
    the size of the node.

    The script runs the task as a child and waits for it, so that it can
    put the task back in the queue (``scontrol requeue``) when the task
    exits with 3, stopped with work left, after Slurm's notice of the time
    limit (SIGUSR1, :data:`NOTICE_SECONDS` before it, which the script
    passes on as SIGTERM) or a preemption (``scontrol show job`` gives a
    ``PreemptTime``). A task stopped with ``scancel`` is not requeued, nor
    one requeued :data:`MAX_REQUEUES` times already. A requeued task starts
    again and skips what it finished.
    """
    slurm = execution.slurm
    options: dict[str, Any] = {
        "job-name": f"runtraj-{_project_slug(project.directory)}",
        # At most n_workers tasks at once; Slurm starts the next as one ends.
        "array": f"0-{n_tasks - 1}"
        + (f"%{slurm.n_workers}" if slurm.n_workers < n_tasks else ""),
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
    lines += [
        "# The task runs as a child, so this script can requeue it. Slurm sends",
        f"# this script USR1 {NOTICE_SECONDS} s before the time limit, which it passes",
        "# on as TERM; the task stops on TERM and exits with 3.",
        "notice=0",
        "on_cluster=${SLURM_CLUSTER_NAME:+-M $SLURM_CLUSTER_NAME}",
        "trap 'notice=1; trapped=1; kill -TERM \"$pid\" 2>/dev/null' USR1",
        "trap 'trapped=1; kill -TERM \"$pid\" 2>/dev/null' TERM INT",
        " ".join(words) + " &",
        "pid=$!",
        "# wait returns early when a trapped signal arrives; wait again then.",
        "while :; do",
        "    trapped=0",
        '    wait "$pid"',
        "    code=$?",
        '    [ "$trapped" = 1 ] || break',
        "done",
        "# 3 is a task stopped with work left: requeue it after the notice or a",
        "# preemption (scontrol shows a PreemptTime), not after scancel.",
        'if [ "$code" = 3 ]; then',
        '    if [ "$notice" = 0 ] && ! scontrol $on_cluster show job "$SLURM_JOB_ID" \\',
        "        | grep -Eq 'PreemptTime=[0-9]'; then",
        '        echo "Stopped, not by the time limit or a preemption; not requeued."',
        f'    elif [ "${{SLURM_RESTART_COUNT:-0}}" -ge {MAX_REQUEUES} ]; then',
        '        echo "Requeued ${SLURM_RESTART_COUNT} times; not again. Run the project'
        ' again, or raise execution.slurm.time."',
        '    elif scontrol $on_cluster requeue "$SLURM_JOB_ID"; then',
        '        echo "Requeued $SLURM_JOB_ID; it starts again and skips what finished."',
        "    fi",
        "fi",
        'exit "$code"',
    ]
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
    folder = write_submission(project, pending, execution, "slurm")
    n_tasks = array_size(
        execution.slurm,
        len(pending),
        max_array_size(execution.slurm.options.get("clusters")),
    )
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


#: Slurm's own ``MaxArraySize``, for a cluster whose ``scontrol`` does not say.
SLURM_MAX_ARRAY_SIZE = 1001


def max_array_size(cluster: str | None = None) -> int:
    """
    Return the most tasks a job array may have on *cluster*: its ``MaxArraySize``, from ``scontrol show config``.

    Slurm's default, :data:`SLURM_MAX_ARRAY_SIZE`, when ``scontrol``
    cannot be run or does not say. An array's indexes must be below it.
    """
    try:
        result = subprocess.run(
            ["scontrol", *_on_cluster(cluster), "show", "config"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return SLURM_MAX_ARRAY_SIZE
    match = re.search(r"^MaxArraySize\s*=\s*(\d+)", result.stdout, re.M)
    return int(match.group(1)) if match else SLURM_MAX_ARRAY_SIZE


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
        return status_after_job(project, releases)

    def describe(self, waits: bool) -> str:
        """Return the line ``runtraj run`` or ``runtraj submit`` prints before it submits."""
        if waits:
            return "Submitting to Slurm and waiting for the job to finish (squeue shows its tasks)"
        return "Submitting to Slurm and returning"


__all__ = [
    "MAX_REQUEUES",
    "NOTICE_SECONDS",
    "SLURM_MAX_ARRAY_SIZE",
    "SlurmBackend",
    "fetch_script",
    "job_script",
    "max_array_size",
    "submit_array",
    "wait",
]

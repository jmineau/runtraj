"""
The PBS backend: a project's unfinished releases as a PBS job array (PBS Pro, OpenPBS).

With ``backend: pbs`` (:class:`PbsBackend`), :func:`runtraj.execution.submit`
writes a job array script into ``_pbs/<stamp>/`` in the project and
submits it with ``qsub`` (:func:`submit_array`). Each subjob runs
``runtraj run --task``, as a Slurm array task does, so a subjob is a
command line that can be read, and run again, by hand. :func:`wait`
follows the array with ``qstat`` until it has finished.

PBS sends no notice before a subjob's ``walltime``, and a user cannot put
a subjob back in the queue, so a subjob that runs out of time stays
stopped; running the project again skips what finished. A preempted
subjob is put back by PBS itself, since the job is rerunnable (``-r y``).
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from runtraj.execution.backends import Backend
from runtraj.execution.config import ExecutionConfig, PbsConfig
from runtraj.execution.runner import (
    _project_slug,
    array_size,
    resolve_compute_root,
    status_after_job,
    write_submission,
)

if TYPE_CHECKING:
    import pandas as pd

    from runtraj.project import Project

logger = logging.getLogger(__name__)

#: PBS's own ``max_array_size``, for a server that does not say.
PBS_MAX_ARRAY_SIZE = 10000


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
    Return the ``qsub`` script of a job array whose subjobs each run a share of the releases.

    Subjob ``i`` runs ``runtraj run <project> --releases
    <folder>/releases.parquet --task i/n_tasks --execution
    <folder>/execution.yaml --cpus <cpus>`` with the Python that called
    this, and writes its log to ``<folder>/<i>.log``. The ``#PBS`` lines
    come from *execution*'s ``pbs`` block: ``-J 0-<n_tasks - 1>%<n_workers>``
    (no ``-J`` for one task), a chunk of ``ncpus=<cpus>`` and ``mem``,
    ``walltime``, ``queue``, ``account``, ``-V`` (the submitting shell's
    environment, as Slurm passes it), ``-r y``, and every other PBS
    resource of the block. Its ``setup`` commands run before the subjob,
    after the script exports ``OMP_NUM_THREADS=1``. With
    ``skip_existing=False`` only a subjob's first start runs again what
    exists; a subjob PBS starts again after a preemption skips it.
    """
    pbs = execution.pbs
    chunk = f"1:ncpus={pbs.cpus}" + (f":mem={pbs.mem}" if pbs.mem else "")
    resources = {"select": chunk, "walltime": pbs.walltime, **pbs.resources}
    array = None
    if n_tasks > 1:
        # At most n_workers subjobs at once; PBS starts the next as one ends.
        array = f"0-{n_tasks - 1}" + (
            f"%{pbs.n_workers}" if pbs.n_workers < n_tasks else ""
        )
    lines = [
        "#!/bin/bash",
        f"#PBS -N runtraj-{_project_slug(project.directory)}",
        *([f"#PBS -J {array}"] if array else []),
        *(
            f"#PBS -l {key}={value}"
            for key, value in resources.items()
            if value is not None
        ),
        *([f"#PBS -q {pbs.queue}"] if pbs.queue else []),
        *([f"#PBS -A {pbs.account}"] if pbs.account else []),
        "#PBS -V",  # the submitting shell's environment, as Slurm passes it
        "#PBS -r y",  # rerunnable, so PBS puts a preempted subjob back
        "#PBS -j oe",
        # PBS's own output files, from before the script writes its log.
        f"#PBS -o {folder}/",
    ]

    command = [
        sys.executable,
        "-m",
        "runtraj",
        "run",
        str(project.directory),
        "--releases",
        str(folder / "releases.parquet"),
        "--task",
        f"$index/{n_tasks}",
        "--execution",
        str(folder / "execution.yaml"),
        "--cpus",
        str(pbs.cpus),
        "--output",
        str(project.output.directory),
    ]
    if compute_root is not None:
        command += ["--compute-root", str(resolve_compute_root(project, compute_root))]
    words = [f'"{w}"' if w.startswith("$") else shlex.quote(w) for w in command]
    log = shlex.quote(str(folder)) + '/"$index.log"'
    started = shlex.quote(str(folder)) + '/"$index.started"'
    lines += [
        "",
        # One job, not an array, when there is one task: its index is 0.
        'index="${PBS_ARRAY_INDEX:-0}"',
        f"exec >> {log} 2>&1",
        'echo "$(date -u +%FT%TZ) $(hostname) job ${PBS_JOBID}"',
        "export OMP_NUM_THREADS=1",
        *pbs.setup,
    ]
    if not skip_existing:
        # A subjob PBS starts again keeps what it finished before.
        lines += [
            'skip="--no-skip"',
            f'[ -e {started} ] && skip=""',
            f"touch {started}",
        ]
        words.append("$skip")
    # exec, so this process is the one PBS signals.
    lines.append("exec " + " ".join(words))
    return "\n".join(lines) + "\n"


def _qsub(script: Path) -> str:
    """Submit *script* with ``qsub`` and return its job id, such as ``12[].server``."""
    result = subprocess.run(
        ["qsub", str(script)], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            "qsub refused the job array: "
            + (result.stderr.strip() or f"exit {result.returncode}")
        )
    return result.stdout.strip()


def max_array_size() -> int:
    """
    Return the most subjobs a job array may have: the server's ``max_array_size``, from ``qstat -Bf``.

    PBS's default, :data:`PBS_MAX_ARRAY_SIZE`, when ``qstat`` cannot be run
    or does not say.
    """
    try:
        result = subprocess.run(
            ["qstat", "-Bf"], capture_output=True, text=True, check=False
        )
    except OSError:
        return PBS_MAX_ARRAY_SIZE
    match = re.search(r"^\s*max_array_size\s*=\s*(\d+)", result.stdout, re.M)
    return int(match.group(1)) if match else PBS_MAX_ARRAY_SIZE


def submit_array(
    project: Project,
    pending: list[str],
    execution: ExecutionConfig,
    skip_existing: bool,
    compute_root: str | Path | None,
) -> str:
    """
    Write a submission folder for *pending* releases, submit its job array, and return its job id.

    The folder is ``_pbs/<date_time>_<id>/`` in the project
    (:func:`runtraj.execution.runner.write_submission`), with ``job.sh``,
    the script given to ``qsub``, and ``job.id``, the job's id.
    """
    n_tasks = array_size(execution.pbs, len(pending), max_array_size())
    folder = write_submission(project, pending, execution, "pbs")
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
    job_id = _qsub(script)
    (folder / "job.id").write_text(job_id + "\n", encoding="utf-8")
    logger.info("Submitted job: %s (%d tasks); logs in %s", job_id, n_tasks, folder)
    return job_id


def _job_state(job_id: str) -> str | None:
    """
    Return the PBS state of a job (``Q``, ``R``, ``B`` for an array that began, ``F`` when finished), or ``None`` when ``qstat`` could not say.

    A finished job that the server keeps no history of is unknown to
    ``qstat``, which is read as finished.
    """
    result = subprocess.run(
        ["qstat", "-x", "-f", "-F", "json", job_id],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        if "Unknown Job Id" in result.stderr or "has finished" in result.stderr:
            return "F"
        return None
    try:
        jobs = json.loads(result.stdout)["Jobs"]
        return next(iter(jobs.values()))["job_state"]
    except (ValueError, KeyError, StopIteration):
        return None


def wait(job_id: str, poll: float = 30.0) -> None:
    """Wait until a PBS job array has finished, polling ``qstat`` every *poll* seconds."""
    while _job_state(job_id) != "F":
        time.sleep(poll)


class PbsBackend(Backend):
    """
    The ``pbs`` backend: the releases as the subjobs of one PBS job array (:func:`submit_array`).

    ``runtraj run`` waits for the array with ``qstat`` (:func:`wait`) and
    then lists the results, once more a minute later when some look
    unfinished (:func:`runtraj.execution.runner.status_after_job`).
    """

    name = "pbs"
    config_class = PbsConfig
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
        """Submit the job array, and return its job id."""
        return submit_array(project, releases, execution, skip_existing, compute_root)

    def run(
        self,
        project: Project,
        releases: list[str],
        execution: ExecutionConfig,
        *,
        skip_existing: bool,
        compute_root: str | Path | None,
    ) -> pd.DataFrame:
        """Submit the job array, wait for it to finish, and return the status table of *releases*."""
        job_id = submit_array(project, releases, execution, skip_existing, compute_root)
        wait(job_id)
        return status_after_job(project, releases)

    def describe(self, waits: bool) -> str:
        """Return the line ``runtraj run`` or ``runtraj submit`` prints before it submits."""
        if waits:
            return "Submitting to PBS and waiting for the job to finish (qstat -t shows its subjobs)"
        return "Submitting to PBS and returning"


__all__ = [
    "PBS_MAX_ARRAY_SIZE",
    "PbsBackend",
    "job_script",
    "max_array_size",
    "submit_array",
    "wait",
]

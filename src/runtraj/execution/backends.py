"""
Where a project's releases run: one class per backend, found by the name ``execution.backend`` gives it.

A backend takes the releases whose results are missing and runs them: in
this process (``local``, :class:`runtraj.execution.runner.LocalBackend`),
as a Slurm job array (``slurm``, :class:`runtraj.execution.slurm.SlurmBackend`),
as a PBS job array (``pbs``, :class:`runtraj.execution.pbs.PbsBackend`)
or as a Kubernetes indexed Job (``kubernetes``,
:class:`runtraj.execution.kubernetes.KubernetesBackend`). A backend in
another package subclasses :class:`Backend`, and ``execution.backend``
names it by the import path of its class, as ``class:`` names a transport
model or a stage:

.. code-block:: yaml

   execution:
     backend: mypkg.backends.QueueBackend
     myqueue:         # the class's name
       queue: short   # a setting of its config_class
       cpus: 4

Every task a backend starts runs the same command, ``runtraj run <project>
--task i/N`` (``pending(project, task=(i, N))``, :func:`runtraj.execution.pending`,
is the releases of task ``i``), so a backend starts tasks and, if it can, waits for them; it never
runs a transport model itself.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

from runtraj._plugins import import_class
from runtraj.exceptions import SchedulerError
from runtraj.execution.config import BackendConfig

if TYPE_CHECKING:
    from pathlib import Path

    import pandas as pd

    from runtraj.execution.config import ExecutionConfig
    from runtraj.project import Project


class Backend(ABC):
    """
    A place a project's releases run: a scheduler that starts ``runtraj run --task i/N`` tasks.

    A subclass sets :attr:`name` and :attr:`config_class` and writes
    :meth:`submit`. A backend that can wait for what it started (``runtraj
    run``) sets :attr:`waits` and writes :meth:`run`; one that can list its
    jobs for ``runtraj status`` writes :meth:`jobs`.

    Examples
    --------
    >>> from runtraj.execution import Backend, BackendConfig
    >>> class QueueConfig(BackendConfig):
    ...     queue: str = "short"
    >>> class QueueBackend(Backend):
    ...     name = "myqueue"
    ...     config_class = QueueConfig
    ...
    ...     def submit(
    ...         self, project, releases, execution, *, skip_existing, compute_root
    ...     ):
    ...         return "job-1"  # start the tasks; return what names them
    >>> QueueBackend().waits
    False
    """

    #: The name of its block under ``execution``, such as ``slurm``.
    name: ClassVar[str]
    #: The class of its block, a :class:`runtraj.execution.BackendConfig`.
    config_class: ClassVar[type[BackendConfig]]
    #: Whether :meth:`run` waits for the tasks, so ``runtraj run`` can use it.
    waits: ClassVar[bool] = False
    #: Whether the releases run in this process, with no tasks to start.
    here: ClassVar[bool] = False
    #: Whether :meth:`submit` takes a list of releases and ``skip_existing=False``.
    #: A backend whose tasks are retried runs every unfinished release and
    #: skips the complete ones, so it takes neither.
    takes_releases: ClassVar[bool] = True

    @abstractmethod
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
        Start the tasks that run *releases*, and return what names them, such as a job id.

        *releases* are the ids of the releases with missing results, in
        project order. Tasks given them (``runtraj run --releases``) share
        them in this order (:func:`runtraj.execution.task_share`); tasks
        given none share every release of the project in the order of a
        hash of their ids (:func:`runtraj.execution.pending`).
        ``execution.block`` is this backend's block. *compute_root* is
        where the workdirs go, or ``None`` for each task's own.
        """

    def run(
        self,
        project: Project,
        releases: list[str],
        execution: ExecutionConfig,
        *,
        skip_existing: bool,
        compute_root: str | Path | None,
    ) -> pd.DataFrame:
        """
        Run *releases* and wait for them, then return their status table (:meth:`runtraj.Project.status`).

        Raises
        ------
        ValueError
            Unless the backend :attr:`waits`.
        """
        raise ValueError(not_waited(self))

    def jobs(
        self, project: Project, execution: ExecutionConfig
    ) -> list[dict[str, Any]] | None:
        """
        Return the project's jobs for ``runtraj status``, oldest first, or ``None`` when it lists none.

        Each job is a dict with ``name``, ``state``, ``reason``, ``created``,
        ``tasks``, ``done``, ``failed``, ``failed_tasks`` and ``running``
        (:func:`runtraj.execution.kubernetes.job_summary`).

        Raises
        ------
        SchedulerError
            When the jobs cannot be listed; ``runtraj status`` prints the
            message and the counts.
        """
        return None

    def describe(self, waits: bool) -> str:
        """Return the line ``runtraj run`` and ``runtraj submit`` print on what they do next."""
        if waits:
            return f"Submitting to {self.name} and waiting for the tasks to finish"
        return f"Submitting to {self.name} and returning"


def not_waited(backend: Backend) -> str:
    """Return why ``run()`` refuses a backend that does not wait for its tasks."""
    return (
        f"run() waits for its runs, and runtraj does not wait for the tasks of "
        f"backend {backend.name!r}. Use submit() (runtraj submit), and runtraj "
        "status to follow it."
    )


#: How long a backend's ``wait`` goes on when the scheduler gives no answer
#: about the job (``sacct`` or ``qstat`` failing, or not knowing it), before
#: it stops with an error: long enough for an accounting database that is
#: slow to hear of a new job, short of waiting for ever where it never will.
SILENT_SCHEDULER_SECONDS = 1800


def check_scheduler_answered(
    answered: float, job_id: str, asked: str, see: str
) -> None:
    """
    Raise :class:`~runtraj.exceptions.SchedulerError` once the scheduler has been silent too long about *job_id*.

    *answered* is when it last said something (``time.monotonic()``),
    *asked* names who was asked, with its verb (``"qstat has"``), and *see*
    is the command to look at the job with.
    """
    if time.monotonic() - answered >= SILENT_SCHEDULER_SECONDS:
        raise SchedulerError(
            f"{asked} said nothing about job {job_id} for "
            f"{SILENT_SCHEDULER_SECONDS // 60} minutes, so runtraj stopped "
            f"waiting for it. The job may still be running: see {see}, and "
            "runtraj status once it ends."
        )


#: The backends built in, by the name ``execution.backend`` gives them, and
#: the import path of each one's class. Any other backend is named by its
#: own import path.
BACKENDS: dict[str, str] = {
    "local": "runtraj.execution.runner.LocalBackend",
    "slurm": "runtraj.execution.slurm.SlurmBackend",
    "pbs": "runtraj.execution.pbs.PbsBackend",
    "kubernetes": "runtraj.execution.kubernetes.KubernetesBackend",
}


def get_backend(name: str) -> Backend:
    """
    Return the backend *name* names.

    *name* is a built-in backend (:data:`BACKENDS`, such as ``slurm``) or
    the import path of a :class:`Backend` subclass, such as
    ``mypkg.backends.MyBackend``, which is imported and made with no
    arguments.

    Examples
    --------
    >>> get_backend("slurm").name
    'slurm'

    Raises
    ------
    ValueError
        If *name* is not a built-in backend and not an import path.
    TypeError
        If the class at the import path is not a :class:`Backend`.
    MissingClass
        If *name* is an import path that cannot be imported on this machine.
    """
    path = BACKENDS.get(name, name)
    if "." not in path:
        raise ValueError(
            f"Unknown backend {name!r}. The built-in backends are "
            f"{', '.join(BACKENDS)}; another is named by the import path of "
            "its class, such as mypkg.backends.MyBackend."
        )
    found = import_class(path)
    if not (isinstance(found, type) and issubclass(found, Backend)):
        raise TypeError(
            f"execution.backend names {path}, which is not a backend: a backend "
            "subclasses runtraj.execution.Backend."
        )
    return found()


__all__ = ["BACKENDS", "Backend", "get_backend", "not_waited"]

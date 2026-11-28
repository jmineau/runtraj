"""
Running simulations: the runner that starts the work and the workers that do it.

:func:`run` runs what is missing here or on Slurm and waits; :func:`submit`
submits it to Slurm or Kubernetes and returns.
Where they run is a :class:`Backend`, which ``execution.backend`` names
(:func:`get_backend`); a backend in another package subclasses it.
:func:`pending` is the releases either would run, or one task of a job array
(``task=(i, n)``). :func:`task_share` splits a list of releases among the tasks.
The Slurm backend is
:mod:`runtraj.execution.slurm` and the Kubernetes one
:mod:`runtraj.execution.kubernetes`; the workers are in
:mod:`runtraj.execution.worker`.
"""

from .backends import Backend, get_backend
from .config import (
    BackendConfig,
    ExecutionConfig,
    FetchConfig,
    KubernetesConfig,
    LocalConfig,
    PbsConfig,
    SlurmConfig,
    Volume,
)
from .runner import pending, run, submit, task_share

__all__ = [
    "Backend",
    "BackendConfig",
    "ExecutionConfig",
    "FetchConfig",
    "KubernetesConfig",
    "LocalConfig",
    "PbsConfig",
    "SlurmConfig",
    "Volume",
    "get_backend",
    "pending",
    "run",
    "submit",
    "task_share",
]

"""
Running simulations: the runner that starts the work and the workers that do it.

:func:`run` runs what is missing here or on Slurm and waits; :func:`submit`
submits it to Slurm or Kubernetes and returns, and :func:`wait` waits for a job array.
Where they run is a :class:`Backend`, which ``execution.backend`` names
(:func:`get_backend`); a backend in another package subclasses it.
:func:`pending` is the releases either would run. :func:`task_share` is which releases one task of a
job array runs, and :func:`job_script` the script such a job array runs, for
a scheduler runtraj does not drive itself. The Slurm backend is
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
from .slurm import fetch_script, job_script, wait

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
    "fetch_script",
    "get_backend",
    "job_script",
    "pending",
    "run",
    "submit",
    "task_share",
    "wait",
]

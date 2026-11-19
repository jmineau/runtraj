"""
Running simulations: the runner that starts the work and the workers that do it.

:func:`run` runs what is missing here or on Slurm and waits; :func:`submit`
submits it to Slurm or Kubernetes and returns, and :func:`wait` waits for a job array.
:func:`pending` is the releases either would run. :func:`task_share` is which releases one task of a
job array runs, and :func:`job_script` the script such a job array runs, for
a scheduler runtraj does not drive itself. The workers are in
:mod:`runtraj.execution.worker`.
"""

from .config import (
    ExecutionConfig,
    FetchConfig,
    KubernetesConfig,
    LocalConfig,
    SlurmConfig,
    Volume,
)
from .runner import fetch_script, job_script, pending, run, submit, task_share, wait

__all__ = [
    "ExecutionConfig",
    "FetchConfig",
    "KubernetesConfig",
    "LocalConfig",
    "SlurmConfig",
    "Volume",
    "fetch_script",
    "job_script",
    "pending",
    "run",
    "submit",
    "task_share",
    "wait",
]

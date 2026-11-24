"""A toy backend that stands for one in another package: it starts nothing and records what it was asked."""

from __future__ import annotations

from typing import Any, ClassVar

import pandas as pd
from pydantic import Field

from runtraj.execution import Backend, BackendConfig

#: What the toy backends were asked, in order: ``(method, releases, execution)``.
CALLS: list[tuple[str, list[str], Any]] = []

#: The jobs :meth:`QueueBackend.jobs` returns.
JOB = {
    "name": "queue-7",
    "state": "running",
    "reason": None,
    "created": "2026-11-24T10:00:00Z",
    "tasks": 4,
    "done": 1,
    "failed": 0,
    "failed_tasks": "",
    "running": 3,
}


class QueueConfig(BackendConfig):
    """The block of :class:`QueueBackend`, ``queue``: a queue name and a number of tasks."""

    queue: str | None = Field(None, description="The queue to submit to. Required.")
    pods: int = Field(4, ge=1, description="Number of tasks.")

    @property
    def tasks(self) -> int:
        """The number of tasks, ``pods``."""
        return self.pods

    def check(self) -> None:
        """Raise when no queue is named."""
        if self.queue is None:
            raise ValueError("execution.queue.queue is required with this backend.")


class QueueBackend(Backend):
    """A scheduler that is not waited for: submit records and returns a job name."""

    name = "queue"
    config_class = QueueConfig

    def submit(self, project, releases, execution, *, skip_existing, compute_root):
        """Record the call, and return a job name."""
        CALLS.append(("submit", list(releases), execution))
        return "queue-7"

    def jobs(self, project, execution):
        """Return one running job."""
        return [JOB]


class WaitingQueueBackend(QueueBackend):
    """The same scheduler, waited for: run records the call and returns every simulation as complete."""

    waits: ClassVar[bool] = True

    def run(self, project, releases, execution, *, skip_existing, compute_root):
        """Record the call, and return a status table of complete simulations."""
        CALLS.append(("run", list(releases), execution))
        return pd.DataFrame({"release": releases, "state": "complete"})


class NotABackend:
    """A class that does not subclass Backend."""

    name = "not"


class TimeoutBackend(QueueBackend):
    """A backend whose block would be ``timeout``, a setting of execution."""

    name = "timeout"

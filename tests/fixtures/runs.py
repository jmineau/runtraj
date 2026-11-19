"""
A transport model run that needs no HYSPLIT, for tests of a project's runs.

:class:`FakeRuns` replaces :func:`runtraj.models.run_model` where the
worker calls it, so a run goes through everything else as a real one does:
the worker writes the particles and the log to the output directory, and a
failure record when the run fails.
"""

from __future__ import annotations

from typing import Any

from runtraj.exceptions import SimulationError
from runtraj.models import ModelRun

from .output import fake_particles


class FakeRuns:
    """
    Stands in for the transport model: each run returns a few particles and a log.

    ``ran`` lists the release ids it ran, in order. A release id in ``fail``
    raises :class:`~runtraj.exceptions.SimulationError` with that reason
    instead, as a run that ran out of met does.
    """

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.fail: dict[str, str] = {}

    def __call__(self, model: str, release: Any, *args: Any, **kwargs: Any) -> ModelRun:
        self.ran.append(release.id)
        if release.id in self.fail:
            reason = self.fail[release.id]
            raise SimulationError(
                f"the run stopped ({reason})", reason=reason, log="the run's log\n"
            )
        return ModelRun(fake_particles(release, n=5), log="the run's log\n")

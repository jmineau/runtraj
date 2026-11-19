"""
Exceptions runtraj raises.

Every one is a :class:`RuntrajError`, so ``except runtraj.RuntrajError`` catches
anything runtraj raised on purpose. Each also subclasses the builtin that
describes it, so ``except RuntimeError`` or ``except FileNotFoundError``
keeps working. Plain input checks raise builtins such as ``ValueError``.
"""

from __future__ import annotations


class RuntrajError(Exception):
    """Base class for every exception runtraj raises."""


class SimulationError(RuntrajError, RuntimeError):
    """
    A simulation failed while it ran.

    The worker catches it, records it with the simulation
    (:attr:`runtraj.Simulation.failure`), and goes on to the next one.

    Parameters
    ----------
    message : str
        What went wrong.
    reason : str, optional
        A short name for the cause, such as ``"MET_COVERAGE"`` or
        ``"TIMEOUT"`` (:class:`runtraj.models.hysplit.FailureReason` lists
        HYSPLIT's). ``runtraj status`` counts failures by it.
    log : str, optional
        The transport model's log of the failed run, kept with the
        simulation (:attr:`runtraj.Simulation.log`).

    Attributes
    ----------
    reason : str or None
        The cause as a plain string (an enum member such as a
        ``FailureReason`` is stored by its value), or ``None`` when it has no
        short name.
    log : str
        The model's log, empty when it wrote none.
    """

    reason: str | None = None
    log: str = ""

    def __init__(self, message: str, reason: str | None = None, log: str = ""):
        super().__init__(message)
        if reason is not None:
            self.reason = str(reason)  # a plain string, as the failure record stores it
        self.log = log


class MeteorologyError(SimulationError):
    """The meteorology files a simulation needs could not be found or staged."""

    reason = "MET_COVERAGE"


class HYSPLITNotFoundError(RuntrajError, FileNotFoundError):
    """
    The HYSPLIT executable (``hycs_std``) is missing, or cannot run.

    Raised when ``directory`` has no ``exec/hycs_std`` or it is not
    executable, and, without ``directory``, when no ``hycs_std`` is on
    ``PATH``. runtraj does not include HYSPLIT: get it and set ``directory``
    (under ``models: hysplit:`` in ``config.yaml``) to the HYSPLIT folder,
    or put the folder that holds ``hycs_std`` on ``PATH``.
    """


class MissingClass(RuntrajError, ImportError):
    """
    A class that ``config.yaml`` names cannot be imported here.

    A transport model or a stage in another package is named by its import
    path (``class: mypkg.models.MyModel``), and every machine that opens the
    project needs that package: a settings folder is named from the class's
    own settings, defaults included. The message names the package to
    install. The ``runtraj`` commands end with exit code 2.
    """


class WorkerDied(RuntrajError, RuntimeError):
    """
    A worker process died without Python's say, and the run stopped.

    A run on ``cpus`` above 1 raises it when a worker is killed outright
    (``SIGKILL``, as the kernel's out-of-memory killer sends). The pool
    cannot say which release the worker held, so the message names every
    release that was in progress. What finished is kept and nothing is marked
    failed: run again, and only what is missing runs. ``runtraj run`` ends
    with exit code 3.
    """


__all__ = [
    "HYSPLITNotFoundError",
    "MeteorologyError",
    "MissingClass",
    "SimulationError",
    "RuntrajError",
    "WorkerDied",
]

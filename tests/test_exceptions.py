"""Tests for runtraj.exceptions: one RuntrajError base, each under its builtin."""

import inspect

import pytest

import runtraj
from runtraj import exceptions
from runtraj.exceptions import (
    HYSPLITNotFoundError,
    MeteorologyError,
    MissingClass,
    RuntrajError,
    SchedulerError,
    SimulationError,
    WorkerDied,
)

#: Each class and the parents it must have.
PARENTS = {
    RuntrajError: (Exception,),
    SimulationError: (RuntrajError, RuntimeError),
    MeteorologyError: (SimulationError, FileNotFoundError),
    HYSPLITNotFoundError: (RuntrajError, FileNotFoundError),
    MissingClass: (RuntrajError, ImportError),
    WorkerDied: (RuntrajError, RuntimeError),
    SchedulerError: (RuntrajError, RuntimeError),
}


def test_every_exception_class_is_in_the_table():
    classes = {
        obj
        for _, obj in inspect.getmembers(exceptions, inspect.isclass)
        if issubclass(obj, BaseException) and obj.__module__ == exceptions.__name__
    }
    assert classes == set(PARENTS)
    assert set(exceptions.__all__) == {cls.__name__ for cls in PARENTS}


@pytest.mark.parametrize("cls", list(PARENTS), ids=lambda cls: cls.__name__)
def test_parents(cls):
    assert cls.__bases__ == PARENTS[cls]
    assert issubclass(cls, RuntrajError)


def test_runtraj_error_is_exported():
    assert runtraj.RuntrajError is RuntrajError


def test_simulation_error_keeps_its_reason_and_log():
    error = SimulationError("no particles", reason="NO_PARTICLE_DATA", log="the log")
    assert (str(error), error.reason, error.log) == (
        "no particles",
        "NO_PARTICLE_DATA",
        "the log",
    )
    assert MeteorologyError("no met").reason == "MET_COVERAGE"


def test_hysplit_not_found_is_not_a_failed_run():
    """A missing executable is a setup problem, so a worker reports it as an error."""
    assert not issubclass(HYSPLITNotFoundError, SimulationError)


def test_a_simulation_error_carries_its_reason():
    from runtraj.models.hysplit import FailureReason

    err = SimulationError(
        "HYSPLIT failed (MET_COVERAGE).", reason=FailureReason.MET_COVERAGE
    )
    assert err.reason == "MET_COVERAGE"
    assert type(err.reason) is str  # the failure record's YAML writes only plain types
    assert SimulationError("no short name").reason is None


def test_a_missing_class_and_a_dead_worker_keep_their_builtins():
    assert issubclass(MissingClass, ImportError)
    assert issubclass(WorkerDied, RuntimeError)

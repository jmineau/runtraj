"""Tests for runtraj fetch stopped by a signal: SIGTERM as a pod or Slurm stops it, and Ctrl-C."""

import os
import signal
import threading
import time

import pytest

from runtraj.cli import _stop_once
from tests.fixtures.platform import not_on_windows

# ---------------------------------------------------------------------------
# _stop_once, in this process
# ---------------------------------------------------------------------------


@pytest.fixture
def handlers():
    """Put SIGINT and SIGTERM back as they were: ``_stop_once`` leaves them ignored after a stop."""
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    yield before
    for sig, handler in before.items():
        signal.signal(sig, handler)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
@not_on_windows("the test sends POSIX signals to its own process")
def test_the_first_stop_raises_and_the_next_are_ignored(handlers, sig):
    with pytest.raises(KeyboardInterrupt), _stop_once():
        os.kill(os.getpid(), sig)
        time.sleep(1)  # the signal is handled here

    os.kill(os.getpid(), signal.SIGINT)  # ignored: this would raise or end the test
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(0.1)
    assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN


def test_a_block_that_is_not_stopped_gives_the_handlers_back(handlers):
    with _stop_once():
        assert signal.getsignal(signal.SIGTERM) is not handlers[signal.SIGTERM]

    assert signal.getsignal(signal.SIGTERM) is handlers[signal.SIGTERM]
    assert signal.getsignal(signal.SIGINT) is handlers[signal.SIGINT]


def test_stop_once_does_nothing_outside_the_main_thread(handlers):
    seen = []

    def work():
        with _stop_once():
            seen.append(signal.getsignal(signal.SIGTERM))

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()

    assert seen == [handlers[signal.SIGTERM]]

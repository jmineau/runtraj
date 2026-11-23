"""Stopping a pool ends the transport model runs its workers started, on every platform."""

import multiprocessing
import os
import signal
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from runtraj.execution import worker
from runtraj.models.hysplit.driver import run_hycs_std


def _write_stand_in(workdir: Path) -> None:
    """Write a ``hycs_std`` that writes ``started``, waits about 3 s, then writes ``finished``."""
    if sys.platform == "win32":
        (workdir / "hycs_std.cmd").write_text(
            "@echo off\r\necho.>started\r\nping -n 4 127.0.0.1 >nul\r\necho.>finished\r\n",
            newline="",
        )
        return
    exe = workdir / "hycs_std"
    exe.write_text("#!/bin/sh\ntouch started\nsleep 3\ntouch finished\n")
    exe.chmod(0o755)


def _run_in_worker(workdir: str) -> None:
    """Run the stand-in in a pool worker, which stops on SIGTERM as runtraj's do (``_pool_run``)."""
    signal.signal(signal.SIGTERM, worker._exit_worker)
    try:
        run_hycs_std(Path(workdir))
    except SystemExit:
        os._exit(1)


def test_stopping_the_pool_ends_the_transport_model_its_worker_started(tmp_path):
    """On Windows a stopped worker's HYSPLIT used to run on: terminate ends the worker alone."""
    _write_stand_in(tmp_path)
    executor = ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"))
    executor.submit(_run_in_worker, str(tmp_path))
    deadline = time.monotonic() + 60
    while not (tmp_path / "started").exists():
        assert time.monotonic() < deadline, "the stand-in hycs_std never started"
        time.sleep(0.1)

    worker._stop_workers(executor)
    time.sleep(5)  # longer than the stand-in waits

    assert not (tmp_path / "finished").exists()

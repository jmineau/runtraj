"""Stopping a pool, or killing a worker outright, ends the transport model runs its workers started, on every platform."""

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
    _wait_for_start(tmp_path)

    worker._stop_workers(executor)
    time.sleep(5)  # longer than the stand-in waits

    assert not (tmp_path / "finished").exists()


def _wait_for_start(workdir: Path) -> None:
    deadline = time.monotonic() + 60
    while not (workdir / "started").exists():
        assert time.monotonic() < deadline, "the stand-in hycs_std never started"
        time.sleep(0.1)


def _run_stand_in(workdir: str) -> None:
    run_hycs_std(Path(workdir))


def test_a_worker_killed_outright_ends_the_transport_model_it_started(tmp_path):
    """A worker the out-of-memory killer ends runs no finally block; its HYSPLIT used to run on."""
    _write_stand_in(tmp_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_run_stand_in, args=(str(tmp_path),)
    )
    process.start()
    _wait_for_start(tmp_path)

    process.kill()  # SIGKILL; TerminateProcess on Windows
    process.join()
    time.sleep(5)  # longer than the stand-in waits

    assert not (tmp_path / "finished").exists()


def test_a_finished_run_leaves_no_process_behind(tmp_path, monkeypatch):
    """The watchdog of a run that ended is gone too, so it can kill nothing later."""
    import subprocess

    started = []

    class Recorded(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started.append(self)

    monkeypatch.setattr(subprocess, "Popen", Recorded)
    _write_stand_in(tmp_path)

    run_hycs_std(tmp_path)

    assert (tmp_path / "finished").exists()
    assert len(started) == (
        1 if sys.platform == "win32" else 2
    )  # HYSPLIT, its watchdog
    assert all(proc.returncode is not None for proc in started)

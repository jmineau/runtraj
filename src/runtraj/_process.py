"""
Starting a transport model's program so that it never outlives the process that started it.

A model runs in a session of its own (:func:`own_group`), so that a Ctrl-C
meant for runtraj does not reach it half way; runtraj then stops it itself
(:func:`terminate`), and a watchdog ends it when the process that started it
dies outright (:func:`ended_if_this_process_dies`). HYSPLIT and FLEXPART
both start theirs this way.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)


def own_group() -> dict[str, Any]:
    """Return the ``Popen`` arguments that start a transport model in a session of its own (a new process group on Windows)."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


#: The watchdog of a transport model's run on POSIX: it reads the process group to
#: kill, then waits for its standard input to close, which happens when the
#: process that started it ends however it ends, and kills the group. ``kill
#: -9 -PGID`` is the form both bash and dash read as a group.
_WATCHDOG = 'read group && read line; kill -9 -"$group" 2>/dev/null'


@contextmanager
def ended_if_this_process_dies() -> Iterator[Callable[[subprocess.Popen[Any]], None]]:
    """
    Yield a call that makes a transport model end if this process dies while the block runs, even when it is killed outright.

    A worker the out-of-memory killer or ``kill -9`` ends runs no
    ``finally`` block, and the model, in a session of its own
    (:func:`own_group`), would run on. Call the yielded function with the
    model's process as soon as it starts. On POSIX a watchdog shell,
    started before the model and in a session of its own, is sent the
    model's process group, then reads a pipe from this process, which the
    system closes when this process dies; it then kills the group. On
    Windows the model is put in a job object that ends it when the job's
    one handle, held here, closes. A process killed between the model's
    start and the call still leaves it running. Where neither can be set
    up, the model runs without it, with a warning.
    """
    if sys.platform == "win32":
        jobs: list[int] = []

        def join_job(proc: subprocess.Popen[Any]) -> None:
            job = _job_that_ends(proc.pid)
            if job is not None:
                jobs.append(job)

        try:
            yield join_job
        finally:
            for job in jobs:
                _close_handle(job)  # the model has ended, or is ended now
        return
    try:
        watchdog = subprocess.Popen(
            ["/bin/sh", "-c", _WATCHDOG],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # so Ctrl-C to this process leaves it
        )
    except OSError as e:
        logger.warning("The transport model runs without a watchdog: %s", e)
        yield lambda proc: None
        return
    pipe = watchdog.stdin
    assert pipe is not None

    def tell_watchdog(proc: subprocess.Popen[Any]) -> None:
        try:
            pipe.write(f"{proc.pid}\n".encode())
            pipe.flush()
        except OSError as e:  # the watchdog has ended
            logger.warning("The transport model runs without a watchdog: %s", e)

    try:
        yield tell_watchdog
    finally:
        watchdog.kill()  # before its pipe closes, so it kills nothing
        watchdog.wait()
        pipe.close()


if sys.platform == "win32":  # Windows' job objects, through ctypes
    #: The limit that ends a job's processes when its last handle closes (``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``).
    _KILL_ON_JOB_CLOSE = 0x2000
    #: The class of ``SetInformationJobObject`` the limit is in (``JobObjectExtendedLimitInformation``).
    _EXTENDED_LIMITS = 9
    #: The access to a process that putting it in a job needs (``PROCESS_SET_QUOTA | PROCESS_TERMINATE``).
    _JOIN_ACCESS = 0x0101

    def _job_that_ends(pid: int) -> int | None:
        """
        Put the Windows process *pid* in a new job object that ends it when the job closes; return the job's handle.

        The job closes when its last handle does, and its one handle is held by
        this process, so the system ends *pid* when this process ends, however
        it ends. Returns ``None`` where the job cannot be made.
        """
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            logger.warning(
                "The transport model runs outside a job object: %s",
                ctypes.WinError(ctypes.get_last_error()),
            )
            return None
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        process = kernel32.OpenProcess(_JOIN_ACCESS, False, pid)
        joined = (
            process
            and kernel32.SetInformationJobObject(
                job, _EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)
            )
            and kernel32.AssignProcessToJobObject(job, process)
        )
        error = ctypes.get_last_error()
        if process:
            kernel32.CloseHandle(process)
        if not joined:
            kernel32.CloseHandle(job)
            logger.warning(
                "The transport model runs outside a job object: %s",
                ctypes.WinError(error),
            )
            return None
        return job

    def _close_handle(handle: int) -> None:
        """Close a Windows handle."""
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(handle)


def terminate(proc: subprocess.Popen[Any]) -> None:
    """Stop a transport model's process (its group, on POSIX) with SIGTERM, then SIGKILL if it does not exit."""
    try:
        _signal_group(proc, hard=False)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        _signal_group(proc, hard=True)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.wait()


def _signal_group(proc: subprocess.Popen[Any], *, hard: bool) -> None:
    """
    Send SIGKILL (*hard*) or SIGTERM to *proc*'s process group.

    Windows has no process group to signal: ``terminate`` and ``kill`` end
    the process alone, which is all HYSPLIT is: it starts no other.
    """
    if sys.platform == "win32":
        try:
            if hard:
                proc.kill()
            else:
                proc.terminate()
        except OSError as e:  # the process has already ended
            raise ProcessLookupError from e
        return
    os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)

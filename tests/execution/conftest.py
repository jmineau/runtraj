"""Fixtures of the execution tests."""

import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def commands(monkeypatch):
    """Record the commands the runner runs; ``answers`` maps a program to its outputs in turn."""
    ran: list[list[str]] = []
    answers: dict[str, list[SimpleNamespace]] = {}

    def fake_run(args, **kwargs):
        ran.append(list(args))
        queue = answers.get(args[0], [])
        return (
            queue.pop(0)
            if queue
            else SimpleNamespace(returncode=0, stdout="", stderr="")
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    # Run inside a Slurm job, the tests would see that job's cluster.
    monkeypatch.delenv("SLURM_CLUSTER_NAME", raising=False)
    return SimpleNamespace(ran=ran, answers=answers)

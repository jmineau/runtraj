"""What a test needs of the platform: a marker for the tests Windows cannot run."""

from __future__ import annotations

import sys

import pytest

WINDOWS = sys.platform == "win32"


def not_on_windows(reason: str) -> pytest.MarkDecorator:
    """Skip a test on Windows, saying *reason*: what the test uses that Windows lacks (a POSIX signal, a shell script, Slurm)."""
    return pytest.mark.skipif(WINDOWS, reason=reason)

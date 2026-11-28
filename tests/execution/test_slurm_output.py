"""
What Slurm itself prints, which runtraj reads: ``sacct``, ``scontrol`` and ``sbatch --parsable``.

The other Slurm tests answer with text the tests wrote, so a change in
Slurm's own would pass them. These are copies of real output, from Slurm
24.11.5 on 2026-11-27: a job array of five tasks, which exited with 0, 3, 1,
and were cancelled and timed out. Only the user id after ``CANCELLED by`` was
changed. ``sacct`` was called as :func:`runtraj.execution.slurm.wait` calls it.
"""

from __future__ import annotations

import re

from runtraj.execution import slurm
from runtraj.execution.config import ExecutionConfig
from runtraj.execution.slurm import job_script, max_array_size

from ..fixtures.slurm import out as _out
from ..fixtures.slurm import project_at as _project_at

#: ``sacct -j ID -X -n -P --format=JobID,State,ExitCode`` as the array went from queued to ended.
SACCT = [
    "9_[0-4]|PENDING|0:0\n",
    "".join(f"9_{i}|RUNNING|0:0\n" for i in range(5)),
    "9_0|COMPLETED|0:0\n9_1|FAILED|3:0\n9_2|CANCELLED by 1001|0:0\n"
    "9_3|RUNNING|0:0\n9_4|FAILED|1:0\n",
    "9_0|COMPLETED|0:0\n9_1|FAILED|3:0\n9_2|CANCELLED by 1001|0:0\n"
    "9_3|TIMEOUT|0:0\n9_4|FAILED|1:0\n",
]

#: The line of ``scontrol show job`` of a running job that says whether it was preempted.
SCONTROL_JOB_PREEMPT = "   PreemptEligibleTime=2026-11-27T19:51:07 PreemptTime=None\n"

#: A line of ``scontrol show config``.
SCONTROL_CONFIG = "MaxArraySize            = 1000\n"


def test_waiting_follows_a_real_job_array_from_the_queue_to_its_end(commands, caplog):
    commands.answers["sacct"] = [_out(text) for text in SACCT]

    with caplog.at_level("WARNING", logger="runtraj.execution.slurm"):
        states = slurm.wait("9", poll=0)

    assert [c[0] for c in commands.ran] == ["sacct"] * 4  # a queued array is waited on
    assert states == {
        "9_0": ("COMPLETED", "0:0"),
        "9_1": ("FAILED", "3:0"),
        "9_2": ("CANCELLED", "0:0"),  # "CANCELLED by 1001" is one state
        "9_3": ("TIMEOUT", "0:0"),
        "9_4": ("FAILED", "1:0"),
    }
    # Exits 1 and 3 are the status table's to report; the rest are logged.
    assert re.findall(r"task (\w+) ended (\w+)", caplog.text) == [
        ("9_2", "CANCELLED"),
        ("9_3", "TIMEOUT"),
    ]


def test_the_array_limit_is_read_from_the_line_slurm_prints(commands):
    commands.answers["scontrol"] = [_out(SCONTROL_CONFIG)]

    assert max_array_size() == 1000


def test_a_job_that_was_not_preempted_does_not_look_preempted_to_the_script(tmp_path):
    """The script requeues a task that exits with 3 only on a notice or a ``PreemptTime=`` with a date."""
    project = _project_at(tmp_path / "proj", output=tmp_path / "out")
    script = job_script(
        project,
        ExecutionConfig(backend="slurm", slurm={"time": "01:00:00"}),
        tmp_path,
        2,
    )
    (pattern,) = re.findall(r"grep -Eq '(PreemptTime=[^']*)'", script)

    assert re.search(pattern, SCONTROL_JOB_PREEMPT) is None
    assert re.search(
        pattern, SCONTROL_JOB_PREEMPT.replace("None", "2026-11-27T20:00:00")
    )

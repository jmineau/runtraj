"""Tests for runtraj.execution.slurm: the job array script, and a task that Slurm stops."""

from __future__ import annotations

import os
import signal
import subprocess

import pytest

from runtraj.execution import runner, slurm
from runtraj.execution.config import ExecutionConfig
from runtraj.execution.slurm import job_script
from tests.fixtures.platform import not_on_windows

from ..fixtures.slurm import hourly_project as _hourly_project
from ..fixtures.slurm import out as _out
from ..fixtures.slurm import project_at as _project_at
from ..fixtures.slurm import sbatch_options as _sbatch

# ---------------------------------------------------------------------------
# The job array script
# ---------------------------------------------------------------------------


@not_on_windows("Slurm and its job scripts are for Linux")
def test_job_script_asks_for_what_the_execution_settings_say(tmp_path):
    project = _project_at(tmp_path / "My_Project", output=tmp_path / "out")
    folder = tmp_path / "My_Project" / "_slurm" / "stamp"
    execution = ExecutionConfig(
        backend="slurm",
        slurm={
            "n_workers": 50,
            "cpus": 4,
            "time": "02:00:00",
            "mem": "8G",
            "partition": "lin-np",
            "account": "lin-np",
            "qos": "lin-np",
            "array_parallelism": 10,
            "setup": ["module load gcc", "export OMP_NUM_THREADS=2"],
            "exclude": "notch345",
            "signal": "B:USR1@300",
            "exclusive": True,
        },
    )

    script = job_script(project, execution, folder, 7)

    assert script.startswith("#!/bin/bash\n")
    assert _sbatch(script) == {
        "job-name": "runtraj-my-project",
        "array": "0-6%10",
        "cpus-per-task": "4",
        "time": "02:00:00",  # as given
        "mem": "8G",
        "partition": "lin-np",
        "account": "lin-np",
        "qos": "lin-np",
        "output": str(folder / "%a.log"),
        "open-mode": "append",
        "requeue": True,
        "signal": "B:USR1@300",  # the slurm options replace the defaults
        "exclude": "notch345",
        "exclusive": True,
    }
    lines = script.splitlines()
    # The log says where and when the task ran, before any setup can fail.
    header = lines.index("module load gcc") - 2
    assert lines[header].startswith('echo "$(date -u +%FT%TZ) $SLURMD_NODENAME job ')
    # The script's own OMP_NUM_THREADS comes before setup, so a setup line has the last word.
    assert lines.index("export OMP_NUM_THREADS=1") < lines.index("module load gcc")
    assert lines.index("module load gcc") < lines.index("export OMP_NUM_THREADS=2")
    last = lines[-1]
    assert last.startswith("exec ")
    assert f"-m runtraj run {project.directory} " in last
    assert f"--releases {folder / 'releases.parquet'}" in last
    assert '--task "$SLURM_ARRAY_TASK_ID/7"' in last
    assert f"--execution {folder / 'execution.yaml'}" in last
    # A task runs its share here, so the script tells it the array's cpus.
    assert "--cpus 4" in last
    # Each task writes where the submitting process does, not to config.yaml's output.
    assert f"--output {(tmp_path / 'out').resolve()}" in last
    assert "--no-skip" not in script
    assert "--compute-root" not in script  # each node uses its own scratch


def test_job_script_leaves_out_what_was_not_set(tmp_path):
    script = job_script(
        _project_at(tmp_path, output=tmp_path / "out"),
        ExecutionConfig(backend="slurm", slurm={"time": "01:00:00"}),
        tmp_path,
        1,
    )
    options = _sbatch(script)
    assert options["array"] == "0-0"
    assert options["signal"] == f"B:USR1@{slurm.NOTICE_SECONDS}"
    for unset in ("mem", "partition", "account", "qos"):
        assert unset not in options


@not_on_windows("Slurm and its job scripts are for Linux")
def test_job_script_runs_again_only_on_the_first_start_and_names_a_compute_root(
    tmp_path,
):
    script = job_script(
        _project_at(tmp_path, output=tmp_path / "out"),
        ExecutionConfig(backend="slurm", slurm={"time": "01:00:00"}),
        tmp_path,
        2,
        skip_existing=False,
        compute_root=tmp_path / "scratch",
    )
    assert '[ "${SLURM_RESTART_COUNT:-0}" -gt 0 ] && skip=""' in script
    assert script.splitlines()[-1].endswith(
        f"--compute-root {(tmp_path / 'scratch').resolve()} $skip"
    )


def _omp_threads_after_setup(script: str) -> str:
    """What OMP_NUM_THREADS is when the script reaches its last line, run in bash."""
    before_exec = script.rstrip("\n").rpartition("\n")[0]
    done = subprocess.run(
        ["bash", "-c", before_exec + '\necho "${OMP_NUM_THREADS-unset}"'],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"]},
    )
    return done.stdout.splitlines()[-1]


@not_on_windows("Slurm and its job scripts are for Linux")
def test_job_script_gives_each_worker_one_thread_of_the_tasks_cpus(tmp_path):
    # Four workers on four CPUs: without this a worker's BLAS sizes its pool by the node.
    script = job_script(
        _project_at(tmp_path, output=tmp_path / "out"),
        ExecutionConfig(backend="slurm", slurm={"time": "01:00:00", "cpus": 4}),
        tmp_path,
        1,
    )
    assert "export OMP_NUM_THREADS=1" in script.splitlines()
    assert _omp_threads_after_setup(script) == "1"


@not_on_windows("Slurm and its job scripts are for Linux")
def test_job_script_shares_cpus_a_slurm_option_adds_among_the_workers(tmp_path):
    # 2 workers on 8 CPUs (cpus-per-task replaced in `slurm`): 4 threads each.
    script = job_script(
        _project_at(tmp_path, output=tmp_path / "out"),
        ExecutionConfig(
            backend="slurm", slurm={"time": "01:00:00", "cpus": 2, "cpus_per_task": 8}
        ),
        tmp_path,
        1,
    )
    assert _omp_threads_after_setup(script) == "4"


@not_on_windows("Slurm and its job scripts are for Linux")
def test_job_script_gives_one_thread_when_cpus_per_task_is_not_a_number(tmp_path):
    # sbatch refuses the value; writing the script must not fail first.
    script = job_script(
        _project_at(tmp_path, output=tmp_path / "out"),
        ExecutionConfig(
            backend="slurm", slurm={"time": "01:00:00", "cpus_per_task": "many"}
        ),
        tmp_path,
        1,
    )
    assert _omp_threads_after_setup(script) == "1"


@not_on_windows("Slurm and its job scripts are for Linux")
def test_job_script_leaves_the_thread_count_to_a_setup_line(tmp_path):
    script = job_script(
        _project_at(tmp_path, output=tmp_path / "out"),
        ExecutionConfig(
            backend="slurm",
            slurm={
                "time": "01:00:00",
                "cpus": 4,
                "setup": ["export OMP_NUM_THREADS=3"],
            },
        ),
        tmp_path,
        1,
    )
    assert _omp_threads_after_setup(script) == "3"


@not_on_windows("Slurm and its job scripts are for Linux")
def test_job_script_is_valid_bash(tmp_path):
    script = tmp_path / "job.sh"
    script.write_text(
        job_script(
            _project_at(tmp_path / "with space", output=tmp_path / "out"),
            ExecutionConfig(
                backend="slurm", slurm={"time": "01:00:00", "setup": ["echo ready"]}
            ),
            tmp_path / "with space" / "_slurm",
            3,
            skip_existing=False,
        )
    )
    subprocess.run(["bash", "-n", str(script)], check=True)


# ---------------------------------------------------------------------------
# A task that is told to stop
# ---------------------------------------------------------------------------


@not_on_windows("Slurm tells a task to stop with SIGUSR1, which Windows lacks")
def test_a_task_told_to_stop_requeues_itself(tmp_path, monkeypatch, commands):
    from runtraj.execution import worker

    project = _hourly_project(tmp_path, 1)
    monkeypatch.setenv("SLURM_JOB_ID", "4321")
    monkeypatch.delenv("SLURM_RESTART_COUNT", raising=False)

    def stopped(*args, **kwargs):
        signal.raise_signal(signal.SIGUSR1)

    def told_twice(*args, **kwargs):
        try:
            stopped()
        finally:
            signal.raise_signal(signal.SIGUSR1)  # a second one only adds to the record

    monkeypatch.setattr(worker, "run_releases", told_twice)
    table = runner.run(project, task=(0, 1))

    assert commands.ran == [["scontrol", "requeue", "4321"]]
    assert set(table["state"]) == {"pending"}
    assert signal.getsignal(signal.SIGUSR1) is signal.SIG_DFL


@not_on_windows("Slurm tells a task to stop with SIGUSR1, which Windows lacks")
def test_a_task_requeues_itself_only_so_many_times(tmp_path, monkeypatch, commands):
    from runtraj.execution import worker

    project = _hourly_project(tmp_path, 1)
    monkeypatch.setenv("SLURM_JOB_ID", "4321")
    monkeypatch.setenv("SLURM_RESTART_COUNT", str(slurm.MAX_REQUEUES))
    monkeypatch.setattr(
        worker, "run_releases", lambda *a, **k: signal.raise_signal(signal.SIGUSR1)
    )
    runner.run(project, task=(0, 1))
    assert commands.ran == []


@not_on_windows("Slurm tells a task to stop with SIGUSR1, which Windows lacks")
def test_a_preempted_task_requeues_itself_and_a_cancelled_one_does_not(
    tmp_path, monkeypatch, commands
):
    """A preempted task gets SIGTERM, and maybe no SIGUSR1 before its grace time ends."""
    from runtraj.execution import worker

    project = _hourly_project(tmp_path, 1)
    monkeypatch.setenv("SLURM_JOB_ID", "4321")
    monkeypatch.delenv("SLURM_RESTART_COUNT", raising=False)
    # run_releases stops on SIGTERM and returns with the release unfinished.
    monkeypatch.setattr(worker, "run_releases", lambda *a, **k: None)

    commands.answers["scontrol"] = [
        _out("JobId=4321 PreemptTime=2026-10-06T12:00:00 Requeue=1")
    ]
    runner.run(project, task=(0, 1))
    assert commands.ran == [
        ["scontrol", "show", "job", "4321"],
        ["scontrol", "requeue", "4321"],
    ]

    commands.ran.clear()
    commands.answers["scontrol"] = [_out("JobId=4321 PreemptTime=None Requeue=1")]
    runner.run(project, task=(0, 1))  # scancel: not preempted
    assert commands.ran == [["scontrol", "show", "job", "4321"]]


@not_on_windows("Slurm tells a task to stop with SIGUSR1, which Windows lacks")
def test_a_task_on_another_cluster_asks_and_requeues_there(
    tmp_path, monkeypatch, commands
):
    from runtraj.execution import worker

    project = _hourly_project(tmp_path, 1)
    monkeypatch.setenv("SLURM_JOB_ID", "4321")
    monkeypatch.setenv("SLURM_CLUSTER_NAME", "kingspeak")
    monkeypatch.delenv("SLURM_RESTART_COUNT", raising=False)
    monkeypatch.setattr(worker, "run_releases", lambda *a, **k: None)
    commands.answers["scontrol"] = [
        _out("JobId=4321 PreemptTime=2026-10-06T12:00:00 Requeue=1")
    ]

    runner.run(project, task=(0, 1))

    assert commands.ran == [
        ["scontrol", "-M", "kingspeak", "show", "job", "4321"],
        ["scontrol", "-M", "kingspeak", "requeue", "4321"],
    ]


@not_on_windows("Slurm tells a task to stop with SIGUSR1, which Windows lacks")
def test_a_task_that_finished_is_not_requeued(tmp_path, monkeypatch, commands):
    import pandas as pd

    from runtraj.execution import worker

    monkeypatch.setenv("SLURM_JOB_ID", "4321")
    monkeypatch.setattr(
        worker, "run_releases", lambda *a, **k: signal.raise_signal(signal.SIGUSR1)
    )
    monkeypatch.setattr(
        slurm, "_status", lambda project, ids: pd.DataFrame({"state": ["complete"]})
    )
    runner.run(_hourly_project(tmp_path, 1), task=(0, 1))
    assert commands.ran == []


@not_on_windows("Slurm tells a task to stop with SIGUSR1, which Windows lacks")
def test_an_interrupt_that_is_not_the_notice_is_not_swallowed(
    tmp_path, monkeypatch, commands
):
    from runtraj.execution import worker

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "run_releases", interrupted)
    with pytest.raises(KeyboardInterrupt):
        runner.run(_hourly_project(tmp_path, 1), task=(0, 1))
    assert commands.ran == []


def test_a_task_on_a_system_without_sigusr1_notices_nothing(monkeypatch):
    """Windows has no SIGUSR1 and no Slurm to send it: the block runs and records nothing."""
    monkeypatch.delattr(slurm.signal, "SIGUSR1", raising=False)
    with slurm._usr1_noticed() as notice:
        assert not notice

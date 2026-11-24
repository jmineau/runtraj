"""Tests for runtraj.execution.slurm: the job array script, and a task that Slurm stops."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from runtraj.execution import runner, slurm
from runtraj.execution.config import ExecutionConfig, SlurmConfig
from runtraj.execution.slurm import array_size, job_script, max_array_size
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
            "n_workers": 3,
            "cpus": 4,
            "time": "02:00:00",
            "mem": "8G",
            "partition": "lin-np",
            "account": "lin-np",
            "qos": "lin-np",
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
        "array": "0-6%3",  # 7 tasks, at most 3 at once
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
    last = _task_line(script)
    assert last.endswith(" &")  # a child, so the script can requeue it
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
    assert _task_line(script).endswith(
        f"--compute-root {(tmp_path / 'scratch').resolve()} $skip &"
    )


def _task_line(script: str) -> str:
    """The line of the job script that starts the task."""
    return next(line for line in script.splitlines() if " -m runtraj run " in line)


def _omp_threads_after_setup(script: str) -> str:
    """What OMP_NUM_THREADS is when the script starts the task, run in bash."""
    before_task = script.partition("\nnotice=0\n")[0]
    done = subprocess.run(
        ["bash", "-c", before_task + '\necho "${OMP_NUM_THREADS-unset}"'],
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
# How many tasks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "releases", "tasks"),
    [
        pytest.param({"n_workers": 50}, 10_000, 500, id="ten-per-worker"),
        pytest.param({"n_workers": 200}, 10_000, 1000, id="capped-at-the-limit"),
        pytest.param({"n_workers": 50}, 120, 120, id="one-per-release"),
        pytest.param({"n_workers": 4, "n_tasks": 6}, 100, 6, id="n_tasks"),
        pytest.param({"n_workers": 100, "cpus": 8}, 10_000, 625, id="whole-rounds"),
        pytest.param({"n_workers": 2, "cpus": 4}, 12, 3, id="no-one-release-tasks"),
        pytest.param({"n_tasks": 6, "cpus": 4}, 100, 6, id="n_tasks-as-given"),
        pytest.param({}, 0, 1, id="at-least-one"),
    ],
)
def test_a_job_array_has_ten_tasks_per_worker_unless_told(settings, releases, tasks):
    assert array_size(SlurmConfig(**settings), releases, limit=1000) == tasks


def test_more_tasks_than_the_cluster_allows_are_refused():
    with pytest.raises(ValueError, match=r"n_tasks is 2000.*at most 1000 tasks"):
        array_size(SlurmConfig(n_tasks=2000), 10_000, limit=1000)


def test_the_array_limit_is_the_cluster_s_max_array_size(commands):
    commands.answers["scontrol"] = [
        _out(
            "Configuration data as of ...\nMaxArraySize            = 1000\nMaxJobCount = 1000000\n"
        )
    ]
    assert max_array_size("kingspeak") == 1000
    assert commands.ran == [["scontrol", "-M", "kingspeak", "show", "config"]]


def test_the_array_limit_is_slurm_s_default_when_scontrol_does_not_say(
    commands, monkeypatch
):
    assert max_array_size() == slurm.SLURM_MAX_ARRAY_SIZE

    def missing(*args, **kwargs):
        raise FileNotFoundError("scontrol")

    monkeypatch.setattr(subprocess, "run", missing)
    assert max_array_size() == slurm.SLURM_MAX_ARRAY_SIZE


def test_a_job_array_runs_at_most_n_workers_tasks_at_once(tmp_path):
    project = _project_at(tmp_path, output=tmp_path / "out")
    four = ExecutionConfig(backend="slurm", slurm={"time": "01:00:00", "n_workers": 4})
    assert _sbatch(job_script(project, four, tmp_path, 40))["array"] == "0-39%4"
    # As many workers as tasks: nothing to hold back.
    assert _sbatch(job_script(project, four, tmp_path, 3))["array"] == "0-2"


def test_array_parallelism_says_it_is_now_n_workers():
    with pytest.raises(ValueError, match="array_parallelism is now n_workers"):
        SlurmConfig(array_parallelism=10)


# ---------------------------------------------------------------------------
# A task that is told to stop: the job script requeues it
# ---------------------------------------------------------------------------

#: A stand-in for ``runtraj run --task``: it says it started, then works
#: until SIGTERM, on which it exits with 3 as runtraj does.
_STOPPABLE_TASK = """\
trap 'exit 3' TERM
touch "$(dirname "$0")/started"
while :; do sleep 0.05; done
"""

#: A stand-in for ``scontrol``: it logs its arguments, and ``show job``
#: gives ``PreemptTime=$PREEMPT_TIME``.
_SCONTROL = """\
#!/bin/bash
echo "$*" >> "$(dirname "$0")/scontrol.log"
case " $* " in *" show "*) echo "JobId=$SLURM_JOB_ID PreemptTime=$PREEMPT_TIME" ;; esac
"""


def _stand_in_job(tmp_path, task: str) -> tuple[Path, Path]:
    """Write a job script whose task is *task* (bash) and a fake ``scontrol``; return the script and the bin folder."""
    script = job_script(
        _project_at(tmp_path / "p", output=tmp_path / "out"),
        ExecutionConfig(backend="slurm", slurm={"time": "01:00:00"}),
        tmp_path,
        1,
    )
    fake = tmp_path / "task.sh"
    fake.write_text("#!/bin/bash\n" + task)
    fake.chmod(0o755)
    path = tmp_path / "job.sh"
    path.write_text(script.replace(shlex.quote(sys.executable), str(fake), 1))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "scontrol").write_text(_SCONTROL)
    (bin_dir / "scontrol").chmod(0o755)
    return path, bin_dir


def _run_job(
    tmp_path,
    task: str,
    *,
    send: signal.Signals | None = None,
    preempt_time: str = "None",
    **env: str,
) -> tuple[int, list[str], str]:
    """Run a stand-in job script as Slurm would, sending *send* once its task started; return its exit code, the scontrol calls, and its output."""
    script, bin_dir = _stand_in_job(tmp_path, task)
    environ = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SLURM_JOB_ID": "4321",
        "SLURM_ARRAY_TASK_ID": "0",
        "PREEMPT_TIME": preempt_time,
        **env,
    }
    job = subprocess.Popen(
        ["bash", str(script)],
        env=environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if send is not None:
        deadline = time.monotonic() + 20
        while not (tmp_path / "started").exists():
            assert time.monotonic() < deadline, "the task never started"
            time.sleep(0.02)
        job.send_signal(send)
    output, _ = job.communicate(timeout=30)
    log = tmp_path / "bin" / "scontrol.log"
    calls = log.read_text().splitlines() if log.exists() else []
    return job.returncode, calls, output


@not_on_windows("Slurm and its job scripts are for Linux")
def test_a_task_told_its_time_is_near_is_stopped_and_requeued(tmp_path):
    code, calls, output = _run_job(tmp_path, _STOPPABLE_TASK, send=signal.SIGUSR1)

    assert code == 3
    assert calls == ["requeue 4321"]
    assert "Requeued 4321; it starts again" in output


@not_on_windows("Slurm and its job scripts are for Linux")
def test_a_preempted_task_is_requeued_and_a_cancelled_one_is_not(tmp_path):
    """A preempted task gets SIGTERM, and maybe no SIGUSR1 before its grace time ends."""
    preempted = tmp_path / "preempted"
    preempted.mkdir()
    code, calls, _ = _run_job(
        preempted,
        _STOPPABLE_TASK,
        send=signal.SIGTERM,
        preempt_time="2026-10-06T12:00:00",
    )
    assert code == 3
    assert calls == ["show job 4321", "requeue 4321"]

    cancelled = tmp_path / "cancelled"
    cancelled.mkdir()
    code, calls, output = _run_job(cancelled, _STOPPABLE_TASK, send=signal.SIGTERM)
    assert code == 3
    assert calls == ["show job 4321"]
    assert "not requeued" in output


@not_on_windows("Slurm and its job scripts are for Linux")
def test_a_task_is_requeued_only_so_many_times(tmp_path):
    code, calls, output = _run_job(
        tmp_path,
        _STOPPABLE_TASK,
        send=signal.SIGUSR1,
        SLURM_RESTART_COUNT=str(slurm.MAX_REQUEUES),
    )
    assert code == 3
    assert calls == []
    assert f"Requeued {slurm.MAX_REQUEUES} times; not again" in output


@not_on_windows("Slurm and its job scripts are for Linux")
def test_a_task_on_another_cluster_is_asked_about_and_requeued_there(tmp_path):
    code, calls, _ = _run_job(
        tmp_path,
        _STOPPABLE_TASK,
        send=signal.SIGTERM,
        preempt_time="2026-10-06T12:00:00",
        SLURM_CLUSTER_NAME="kingspeak",
    )
    assert code == 3
    assert calls == ["-M kingspeak show job 4321", "-M kingspeak requeue 4321"]


@not_on_windows("Slurm and its job scripts are for Linux")
@pytest.mark.parametrize("exit_code", [0, 1, 2, 4])
def test_a_task_that_ends_by_itself_is_not_requeued(tmp_path, exit_code):
    code, calls, _ = _run_job(tmp_path, f"exit {exit_code}\n")
    assert (code, calls) == (exit_code, [])


@not_on_windows("Slurm and its job scripts are for Linux")
def test_a_task_that_finished_before_the_notice_is_not_requeued(tmp_path):
    finishes_on_term = _STOPPABLE_TASK.replace("exit 3", "exit 0")
    code, calls, _ = _run_job(tmp_path, finishes_on_term, send=signal.SIGUSR1)
    assert (code, calls) == (0, [])


def test_a_stopped_task_raises_and_runs_no_slurm_command(
    tmp_path, monkeypatch, commands
):
    """The worker knows no scheduler: it stops, and runtraj run exits with 3."""
    from runtraj.execution import worker

    monkeypatch.setenv("SLURM_JOB_ID", "4321")

    def stopped(*args, **kwargs):
        raise KeyboardInterrupt  # what the worker raises on SIGTERM

    monkeypatch.setattr(worker, "run_releases", stopped)
    with pytest.raises(KeyboardInterrupt):
        runner.run(_hourly_project(tmp_path, 1), task=(0, 1))
    assert commands.ran == []

"""Tests for runtraj.execution.pbs: the job array script, qsub, and waiting with qstat."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from runtraj.execution import backends, pbs, runner
from runtraj.execution.config import ExecutionConfig, PbsConfig
from runtraj.execution.pbs import job_script, max_array_size
from tests.fixtures.platform import not_on_windows

from ..fixtures.slurm import hourly_project as _hourly_project
from ..fixtures.slurm import out as _out
from ..fixtures.slurm import project_at as _project_at


def _pbs_lines(script: str) -> list[str]:
    """The #PBS lines of a script, without the prefix."""
    return [
        line[len("#PBS ") :] for line in script.splitlines() if line.startswith("#PBS ")
    ]


def _execution(**pbs_settings) -> ExecutionConfig:
    return ExecutionConfig(backend="pbs", pbs={"walltime": "01:00:00", **pbs_settings})


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


def test_a_pbs_run_needs_a_walltime():
    with pytest.raises(ValueError, match=r"execution\.pbs\.walltime is required"):
        ExecutionConfig(backend="pbs")
    ExecutionConfig(pbs={"n_workers": 4})  # an unused block may leave it out


@pytest.mark.parametrize(
    "walltime",
    ["02:00:00", "90:00", "3600", pytest.param(3600, id="3600-as-int"), "100:00:00"],
)
def test_a_walltime_is_what_qsub_takes(walltime):
    assert PbsConfig(walltime=walltime).walltime == walltime


@pytest.mark.parametrize("walltime", ["2h", "1-02:00:00", "01:00:00:00", ""])
def test_a_walltime_qsub_would_refuse_is_refused(walltime):
    with pytest.raises(ValueError, match="Not a PBS walltime"):
        PbsConfig(walltime=walltime)


def test_a_misspelled_setting_is_refused_not_sent_to_qsub():
    with pytest.raises(ValueError, match="Did you mean 'walltime'"):
        PbsConfig(wallltime="01:00:00")


def test_a_pbs_resource_spelled_like_a_setting_is_a_resource():
    resources = {"vmem": "8gb", "pmem": "2gb", "ncpus": 4, "min_walltime": "00:30:00"}
    assert PbsConfig(walltime="01:00:00", **resources).resources == resources


def test_a_pbs_run_has_no_fetch_job():
    with pytest.raises(ValueError, match="fetch"):
        PbsConfig(fetch=True)


def test_ten_subjobs_per_worker_unless_told():
    assert PbsConfig(n_workers=20).tasks == 200
    assert PbsConfig(n_workers=20, n_tasks=30).tasks == 30


# ---------------------------------------------------------------------------
# The job array script
# ---------------------------------------------------------------------------


@not_on_windows("PBS and its job scripts are for Linux")
def test_job_script_asks_for_what_the_execution_settings_say(tmp_path):
    project = _project_at(tmp_path / "My_Project", output=tmp_path / "out")
    folder = tmp_path / "My_Project" / "_pbs" / "stamp"
    execution = _execution(
        n_workers=3,
        cpus=4,
        mem="8gb",
        queue="main",
        account="UUTA0001",
        place="scatter",
        setup=["module load gcc"],
    )

    script = job_script(project, execution, folder, 7)

    assert script.startswith("#!/bin/bash\n")
    assert _pbs_lines(script) == [
        "-N runtraj-my-project",
        "-J 0-6%3",  # 7 subjobs, at most 3 at once
        "-l select=1:ncpus=4:mem=8gb",
        "-l walltime=01:00:00",
        "-l place=scatter",  # another resource, by its name
        "-q main",
        "-A UUTA0001",
        "-V",
        "-r y",
        "-j oe",
        f"-o {folder}/",
    ]
    lines = script.splitlines()
    assert lines.index("export OMP_NUM_THREADS=1") < lines.index("module load gcc")
    last = lines[-1]
    assert last.startswith(f"exec {shlex.quote(sys.executable)} -m runtraj run ")
    assert '--task "$index/7"' in last
    assert f"--releases {folder / 'releases.parquet'}" in last
    assert f"--execution {folder / 'execution.yaml'}" in last
    assert "--cpus 4" in last
    assert "--no-skip" not in script


def test_one_task_is_one_job_not_an_array(tmp_path):
    script = job_script(
        _project_at(tmp_path, output=tmp_path / "out"), _execution(), tmp_path, 1
    )
    assert not any(line.startswith("-J") for line in _pbs_lines(script))


def test_a_select_of_the_block_replaces_runtraj_s_chunk(tmp_path):
    script = job_script(
        _project_at(tmp_path, output=tmp_path / "out"),
        _execution(select="1:ncpus=4:ngpus=1"),
        tmp_path,
        4,
    )
    assert [line for line in _pbs_lines(script) if line.startswith("-l select")] == [
        "-l select=1:ncpus=4:ngpus=1"
    ]


def test_as_many_workers_as_tasks_holds_nothing_back(tmp_path):
    script = job_script(
        _project_at(tmp_path, output=tmp_path / "out"),
        _execution(n_workers=8),
        tmp_path,
        3,
    )
    assert "-J 0-2" in _pbs_lines(script)


def _run_script(tmp_path, script: str, **env: str) -> None:
    """Run a job script in bash with a stand-in for runtraj that prints its arguments to the subjob's log."""
    fake = tmp_path / "python.sh"
    fake.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    path = tmp_path / "job.sh"
    path.write_text(script.replace(shlex.quote(sys.executable), str(fake), 1))
    subprocess.run(
        ["bash", str(path)],
        env={"PATH": os.environ["PATH"], **env},
        check=True,
    )


@not_on_windows("PBS and its job scripts are for Linux")
@pytest.mark.parametrize(("index", "task"), [(None, "0/1"), ("5", "5/7")])
def test_a_subjob_runs_its_share_and_writes_its_log(tmp_path, index, task):
    folder = tmp_path / "with space" / "_pbs"
    folder.mkdir(parents=True)
    n_tasks = int(task.split("/")[1])
    script = job_script(
        _project_at(tmp_path / "p", output=tmp_path / "out"),
        _execution(),
        folder,
        n_tasks,
    )
    env = {"PBS_JOBID": "12[5].server"}
    if index is not None:
        env["PBS_ARRAY_INDEX"] = index
    _run_script(tmp_path, script, **env)

    log = (folder / f"{index or 0}.log").read_text().splitlines()
    assert log[0].endswith(" job 12[5].server")
    args = log[1:]
    assert args[args.index("--task") + 1] == task


@not_on_windows("PBS and its job scripts are for Linux")
def test_no_skip_holds_only_for_a_subjob_s_first_start(tmp_path):
    script = job_script(
        _project_at(tmp_path / "p", output=tmp_path / "out"),
        _execution(),
        tmp_path,
        2,
        skip_existing=False,
    )
    _run_script(tmp_path, script, PBS_ARRAY_INDEX="1")
    _run_script(
        tmp_path, script, PBS_ARRAY_INDEX="1"
    )  # started again, after a preemption
    starts = (tmp_path / "1.log").read_text().split(" job ")
    assert "--no-skip" in starts[1]
    assert "--no-skip" not in starts[2]


# ---------------------------------------------------------------------------
# Submitting and waiting
# ---------------------------------------------------------------------------


def test_submit_writes_a_submission_and_hands_it_to_qsub(commands, tmp_path):
    project = _hourly_project(tmp_path, 3)
    commands.answers["qsub"] = [_out("12[].pbs-server\n")]

    job_id = runner.submit(project, execution=_execution(n_workers=2))

    assert job_id == "12[].pbs-server"
    [(qsub, script)] = commands.ran
    assert qsub == "qsub"
    folder = Path(script).parent
    assert folder.parent == project.directory / "_pbs"
    assert (folder / "job.id").read_text() == "12[].pbs-server\n"
    assert (folder / "releases.txt").read_text().split() == list(
        project.releases["release"]
    )
    # Three releases, so three subjobs (ten per worker would be 20), two at once.
    assert "-J 0-2%2" in _pbs_lines(Path(script).read_text(encoding="utf-8"))


def test_submit_says_why_qsub_refused(commands, tmp_path):
    commands.answers["qsub"] = [_out(stderr="qsub: Unknown queue", returncode=170)]
    with pytest.raises(
        RuntimeError, match="qsub refused the job array: qsub: Unknown queue"
    ):
        runner.submit(_hourly_project(tmp_path, 1), execution=_execution(queue="nope"))


def test_more_subjobs_than_the_server_allows_are_refused(
    commands, tmp_path, monkeypatch
):
    monkeypatch.setattr(pbs, "max_array_size", lambda: 2)
    with pytest.raises(ValueError, match=r"execution\.pbs\.n_tasks is 3"):
        runner.submit(_hourly_project(tmp_path, 4), execution=_execution(n_tasks=3))
    assert commands.ran == []


def _qstat(state: str) -> object:
    return _out(json.dumps({"Jobs": {"12[].pbs-server": {"job_state": state}}}))


def test_wait_polls_qstat_until_the_array_has_finished(commands):
    commands.answers["qstat"] = [
        _out(returncode=1, stderr="cannot connect"),
        _qstat("Q"),
        _qstat("B"),
        _qstat("F"),
    ]
    pbs.wait("12[].pbs-server", poll=0)
    assert commands.ran[-1] == ["qstat", "-x", "-f", "-F", "json", "12[].pbs-server"]
    assert len(commands.ran) == 4


def test_wait_stops_when_qstat_says_nothing_for_too_long(commands, monkeypatch):
    """A qstat that does not take -F json never answers."""
    monkeypatch.setattr(backends, "SILENT_SCHEDULER_SECONDS", 0)
    commands.answers["qstat"] = [
        _out(returncode=2, stderr="qstat: invalid option -- 'F'")
    ]
    with pytest.raises(RuntimeError, match=r"nothing about job 12\[\]\.pbs-server"):
        pbs.wait("12[].pbs-server", poll=0)


def test_a_job_the_server_has_forgotten_has_finished(commands):
    """Without job history, a finished job is unknown to qstat."""
    commands.answers["qstat"] = [
        _out(returncode=153, stderr="qstat: Unknown Job Id 12[].pbs-server")
    ]
    pbs.wait("12[].pbs-server", poll=0)
    assert len(commands.ran) == 1


def test_run_on_pbs_submits_waits_and_lists_the_results(
    commands, tmp_path, monkeypatch
):
    project = _hourly_project(tmp_path, 2)
    commands.answers["qsub"] = [_out("13[].pbs-server\n")]
    commands.answers["qstat"] = [_qstat("F")]
    slept: list[float] = []
    monkeypatch.setattr(runner.time, "sleep", slept.append)

    table = runner.run(project, execution=_execution())

    assert [args[0] for args in commands.ran] == ["qsub", "qstat"]
    assert set(table["state"]) == {"pending"}  # nothing ran: qsub is a fake
    assert slept == [runner.RELIST_SECONDS]  # unfinished, so listed again


def test_the_array_limit_is_the_server_s_max_array_size(commands):
    commands.answers["qstat"] = [
        _out("Server: pbs\n    server_state = Active\n    max_array_size = 2500\n")
    ]
    assert max_array_size() == 2500
    assert commands.ran == [["qstat", "-Bf"]]


def test_the_array_limit_is_pbs_s_default_when_qstat_does_not_say(commands):
    assert max_array_size() == pbs.PBS_MAX_ARRAY_SIZE

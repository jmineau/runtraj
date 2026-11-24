"""Tests for runtraj.cli - Typer command-line interface."""

import datetime as dt
import errno
import ftplib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path

import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from runtraj import Project
from runtraj._transient import is_transient
from runtraj.cli import _resolve_project, app
from runtraj.exceptions import WorkerDied
from tests.fixtures.platform import not_on_windows

from .fixtures.factories import (
    make_met_config,
    make_met_files,
    make_project_config,
    make_release,
)
from .fixtures.stages import chain

runner = CliRunner()

# s3fs's own numbers for errnos that macOS lacks.
_EREMCHG = getattr(errno, "EREMCHG", 78)
_EREMOTEIO = getattr(errno, "EREMOTEIO", 121)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_minimal_config(tmp_path):
    """Write a minimal config.yaml + releases.csv so _resolve_project succeeds."""
    cfg = make_project_config(
        tmp_path,
        # Local, with the time limit a Slurm run of it needs.
        execution={"slurm": {"time": "01:00:00"}},
    )
    cfg.to_yaml(tmp_path / "config.yaml")
    (tmp_path / "releases.csv").write_text(
        "time,longitude,latitude,altitude\n2023-01-01 12:00:00,-111.85,40.77,5.0\n"
    )


NOON = dt.datetime(2023, 1, 1, 12)


def _release(hour: int = 12):
    return make_release(NOON.replace(hour=hour))


def _project(
    tmp_path, releases=None, *, met_files: bool = False, **settings
) -> Project:
    """
    Make a project of particles only, of runs of 3 h, with its output in ``tmp_path / "out"``.

    With *met_files*, the met holds the files a run from noon needs.
    """
    met_dir = tmp_path / "met"
    met = make_met_config(met_dir)
    if met_files:
        make_met_files(met_dir, NOON, -3)
    settings.setdefault("variants", {"hrrr": {}})
    return Project.init(
        tmp_path / "proj",
        [_release(12), _release(13)] if releases is None else releases,
        mets={"hrrr": met},
        models={"hysplit": {"n_hours": -3}},
        output=str(tmp_path / "out"),
        execution={"slurm": {"time": "01:00:00"}},
        **settings,
    )


def _invoke(*args, project: Project | None = None):
    """Run ``runtraj <command> [<project>] <options>`` in this process; *args* are the command, then its options."""
    where = [] if project is None else [str(project.directory)]
    return runner.invoke(app, [*map(str, args[:1]), *where, *map(str, args[1:])])


def _run(project: Project, *args):
    """Run ``runtraj run`` on *project*, with a compute root beside it."""
    root = project.directory.parent / "compute"
    return _invoke("run", "--compute-root", root, *args, project=project)


class _Calls(list):
    """What `runtraj run` asked of the project; ``states`` is the status the fake run returns."""

    states: list[str] = []


@pytest.fixture
def calls(monkeypatch):
    """Record what `runtraj run` asks of the project, and run nothing."""
    recorded = _Calls()

    def run(self, **kwargs):
        recorded.append(("run", kwargs))
        n = len(recorded.states)
        return pd.DataFrame(
            {
                "release": [f"r{i}" for i in range(n)],
                "variant": ["hrrr"] * n,
                "state": recorded.states,
                "reason": [
                    "met_missing" if s == "failed" else None for s in recorded.states
                ],
            }
        )

    def submit(self, **kwargs):
        recorded.append(("submit", kwargs))
        return "12345"

    monkeypatch.setattr("runtraj.cli.Project.run", run)
    monkeypatch.setattr("runtraj.cli.Project.submit", submit)
    return recorded


# ---------------------------------------------------------------------------
# _resolve_project helper
# ---------------------------------------------------------------------------


def test_resolve_project_returns_path_when_config_exists(tmp_path):
    """Returns the resolved path when config.yaml is present."""
    (tmp_path / "config.yaml").write_text("mets:\n  hrrr: /data/hrrr\n")
    resolved = _resolve_project(tmp_path)
    assert resolved == str(tmp_path.resolve())


# ---------------------------------------------------------------------------
# status command
# ---------------------------------------------------------------------------


def test_status_counts_the_simulations_and_lists_no_folders_yet(tmp_path):
    project = _project(tmp_path)
    (tmp_path / "out").mkdir()

    result = _invoke("status", project=project)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == (
        f"Project: {project.directory}  total=2  complete=0  failed=0  "
        "interrupted=0  pending=2"
    )
    assert lines[1] == f"Output: {tmp_path / 'out'}"
    assert lines[2] == "  no settings folders yet"


def test_status_says_when_the_output_directory_does_not_exist(tmp_path):
    project = _project(tmp_path)

    result = _invoke("status", "--output", str(tmp_path / "typo"), project=project)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].endswith("pending=2")
    assert lines[1] == f"Output: {tmp_path / 'typo'}"
    assert "does not exist" in lines[2] and "--output" in lines[2]
    data = json.loads(
        _invoke(
            "status", "--json", "--output", str(tmp_path / "typo"), project=project
        ).output
    )
    assert data["output_exists"] is False


def test_status_counts_each_variant_and_the_failures_by_reason(
    tmp_path, transport_runs
):
    project = _project(tmp_path, variants={"hrrr": {}, "long": {"n_hours": -6}})
    transport_runs.fail[_release(13).id] = "MET_COVERAGE"
    _run(project)

    result = _invoke("status", project=project)

    assert result.exit_code == 0, result.output
    assert "total=4  complete=2  failed=2  interrupted=0  pending=0" in result.output
    assert "  hrrr: total=2  complete=1  failed=1" in result.output
    assert "  long: total=2  complete=1  failed=1" in result.output
    assert "failed: MET_COVERAGE 2  (why: each one's .failure.yaml, under " in (
        result.output
    )
    folders = [line for line in result.output.splitlines() if "settings=" in line]
    assert len(folders) == 2
    assert all(line.startswith("  particles  settings=") for line in folders)
    assert all("1 file" in line for line in folders)


def test_status_as_json(tmp_path, transport_runs):
    project = _project(tmp_path)
    transport_runs.fail[_release(13).id] = "MET_COVERAGE"
    _run(project)

    result = _invoke("status", "--json", project=project)

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    counts = {"total": 2, "complete": 1, "failed": 1, "interrupted": 0, "pending": 0}
    assert data["project"] == str(project.directory)
    assert data["output"] == str(tmp_path / "out")
    assert data["simulations"] == counts
    assert data["variants"] == {"hrrr": counts}
    assert data["failed"] == {"MET_COVERAGE": 1}
    [folder] = data["folders"]
    assert folder["kind"] == "particles"
    assert folder["folder"].startswith("hrrr-")
    assert folder["name"] == "hrrr"
    assert folder["files"] == 1
    assert folder["variants"] == ["hrrr"]
    assert folder["differs"] == ""


def test_status_lists_a_folder_no_variant_uses_apart(tmp_path, transport_runs):
    project = _project(tmp_path)
    _run(project)
    text = project.config_path.read_text().replace("n_hours: -3", "n_hours: -6")
    project.config_path.write_text(text)

    result = _invoke("status", project=project)

    assert result.exit_code == 0, result.output
    assert "pending=2" in result.output
    assert "  Used by this project: none yet" in result.output
    assert "  Not used by this project (changed settings" in result.output
    [folder] = [line for line in result.output.splitlines() if "settings=" in line]
    assert folder.startswith("    particles  settings=hrrr-")
    assert "n_hours" in folder


def test_status_of_a_directory_that_is_not_a_project_exits_2(tmp_path):
    result = _invoke("status", tmp_path)

    assert result.exit_code == 2
    assert "does not look like a project directory" in result.output


def test_status_of_a_project_with_a_bad_release_row_exits_2(tmp_path):
    project = _project(tmp_path)
    project.releases_path.write_text(
        "time,longitude,latitude,altitude\nnot a time,-111.85,40.77,5\n"
    )

    result = _invoke("status", project=project)

    assert result.exit_code == 2
    assert result.output.startswith("Error: ")
    assert "Traceback" not in result.output


def test_status_without_the_met_counts_pending_until_there_are_results(
    tmp_path, transport_runs
):
    project = _project(tmp_path)
    for path in (tmp_path / "met").iterdir():
        path.unlink()

    before = _invoke("status", project=project)

    assert before.exit_code == 0, before.output
    assert "pending=2" in before.output

    make_met_config(tmp_path / "met")
    _run(project)
    for path in (tmp_path / "met").iterdir():
        path.unlink()

    after = _invoke("status", project=project)

    assert after.exit_code == 2
    assert after.output.startswith("Error: ")


# ---------------------------------------------------------------------------
# check command
# ---------------------------------------------------------------------------


def test_check_with_nothing_to_find_exits_0(tmp_path):
    project = _project(tmp_path, [_release(12)], met_files=True)

    result = _invoke("check", project=project)

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"Project: {project.directory}  releases=1",
        "Variants: hrrr (met hrrr, particles only)",
        "No problems found.",
    ]


def test_check_names_the_stages_of_each_variant(tmp_path):
    project = _project(tmp_path, [_release(12)], met_files=True, stages=chain())

    result = _invoke("check", project=project)

    assert "Variants: hrrr (met hrrr, count, double)" in result.output


def test_check_lists_what_it_finds_as_a_table_and_exits_1(tmp_path):
    project = _project(tmp_path, [_release(12), _release(16)], met_files=True)

    result = _invoke("check", project=project)

    assert result.exit_code == 1, result.output
    lines = result.output.splitlines()
    header = next(line for line in lines if line.startswith("check"))
    assert header.split() == ["check", "variant", "release", "message"]
    [row] = [line for line in lines if line.startswith("met_files")]
    assert row.split()[1:3] == ["hrrr", _release(16).id]
    assert lines[-1] == "Found 1 problem: met_files 1"


def test_check_shows_ten_rows_of_a_check_and_counts_the_rest(tmp_path):
    # The next day: none has its met files.
    releases = [make_release(dt.datetime(2023, 1, 2, h)) for h in range(11)]
    project = _project(tmp_path, releases, met_files=True)

    result = _invoke("check", project=project)

    assert result.exit_code == 1
    rows = [line for line in result.output.splitlines() if line.startswith("met_files")]
    assert len(rows) == 11
    assert rows[-1].endswith("... 1 more (project.check() lists them all)")
    assert result.output.rstrip().endswith("Found 11 problems: met_files 11")


def test_check_of_a_project_with_an_invalid_config_exits_2(tmp_path):
    project = _project(tmp_path)
    project.config_path.write_text("mets: [\n")

    result = _invoke("check", project=project)

    assert result.exit_code == 2
    assert "not valid YAML" in result.output


# ---------------------------------------------------------------------------
# fetch command
# ---------------------------------------------------------------------------


def test_fetch_with_every_met_file_there_exits_0(tmp_path):
    project = _project(tmp_path, [_release(12)], met_files=True)

    result = _invoke("fetch", project=project)

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"Project: {project.directory}  releases=1",
        "Met hrrr: 4 files ready",
    ]


def test_fetch_lists_the_times_whose_files_are_missing_and_exits_1(tmp_path):
    project = _project(tmp_path, [_release(12), _release(18)], met_files=True)

    result = _invoke("fetch", project=project)

    assert result.exit_code == 1, result.output
    assert "Met hrrr: could not get the files for these times:" in result.output
    assert "2023-01-01 15:00" in result.output


def test_fetch_for_a_project_that_is_done_needs_nothing(tmp_path, transport_runs):
    project = _project(tmp_path, [_release(18)])
    _run(project)

    result = _invoke("fetch", project=project)

    assert result.exit_code == 0, result.output
    assert "No unfinished simulation needs met files." in result.output


def test_fetch_for_the_releases_in_a_file(tmp_path):
    project = _project(tmp_path, [_release(12), _release(18)], met_files=True)
    ids = tmp_path / "ids.txt"
    ids.write_text(f"{_release(12).id}\n")

    result = _invoke("fetch", "--releases", ids, project=project)

    assert result.exit_code == 0, result.output
    assert f"Project: {project.directory}  releases=1" in result.output


def test_fetch_for_a_release_the_project_does_not_have_exits_2(tmp_path):
    project = _project(tmp_path, met_files=True)
    ids = tmp_path / "ids.txt"
    ids.write_text("nope\n")

    result = _invoke("fetch", "--releases", ids, project=project)

    assert result.exit_code == 2
    assert f"Error: {ids}: " in result.output


# ---------------------------------------------------------------------------
# run command
# ---------------------------------------------------------------------------


def test_run_says_what_it_will_do_runs_here_and_exits_0(tmp_path, transport_runs):
    project = _project(tmp_path)

    result = _run(project)

    assert result.exit_code == 0, result.output
    assert transport_runs.ran == [_release(12).id, _release(13).id]
    lines = result.output.splitlines()
    assert lines[:6] == [
        f"Starting run: project={project.directory}  backend=local  tasks=1  "
        "cpus=1  skip=existing",
        f"Output: {tmp_path / 'out'}",
        f"Compute root: {tmp_path / 'compute'}",
        "Releases loaded: 2",
        "Variants: hrrr",
        "Running here, one line per release as it finishes",
    ]
    assert lines[-1] == (
        f"Project: {project.directory}  total=2  complete=2  failed=0  "
        "interrupted=0  pending=0"
    )


def test_run_with_a_failed_simulation_exits_1(tmp_path, transport_runs):
    project = _project(tmp_path)
    transport_runs.fail[_release(13).id] = "MET_COVERAGE"

    result = _run(project)

    assert result.exit_code == 1, result.output
    assert "complete=1  failed=1" in result.output
    assert "failed: MET_COVERAGE 1" in result.output


def test_run_exits_3_after_a_connection_error_and_1_once_it_is_mended(
    tmp_path, transport_runs, monkeypatch
):
    """The rerun runs what did not finish; a simulation that failed for a real reason stays failed."""
    project = _project(tmp_path)
    transport_runs.fail[_release(13).id] = "MET_COVERAGE"
    down = {_release(12).id}

    def run_model(model, release, *args, **kwargs):
        if release.id in down:
            raise ConnectionError("down")
        return transport_runs(model, release, *args, **kwargs)

    monkeypatch.setattr("runtraj.execution.worker.run_model", run_model)
    first = _run(project)
    assert first.exit_code == 3, first.output
    assert "complete=0  failed=1  interrupted=1  pending=0" in first.output

    down.clear()
    second = _run(project)
    assert second.exit_code == 1, second.output
    assert "complete=1  failed=1  interrupted=0  pending=0" in second.output


def test_run_skips_what_is_complete_unless_no_skip(tmp_path, transport_runs):
    project = _project(tmp_path)
    _run(project)

    again = _run(project)
    no_skip = _run(project, "--no-skip")

    assert again.exit_code == 0 and no_skip.exit_code == 0
    assert "skip=no-skip" in no_skip.output
    assert len(transport_runs.ran) == 4


def test_run_only_the_releases_listed_in_a_file(tmp_path, transport_runs):
    project = _project(tmp_path)
    ids = tmp_path / "ids.txt"
    ids.write_text(f"# the second release\n\n{_release(13).id}\n")

    result = _run(project, "--releases", ids)

    assert result.exit_code == 0, result.output
    assert transport_runs.ran == [_release(13).id]
    assert "Releases listed: 1" in result.output
    assert result.output.splitlines()[-1].startswith("This run:  total=1  complete=1")


def test_a_releases_file_saved_with_a_byte_order_mark_is_read(tmp_path, transport_runs):
    """Excel's "CSV UTF-8" starts the file with one, which became part of the first id."""
    project = _project(tmp_path)
    ids = tmp_path / "ids.txt"
    ids.write_text(f"{_release(13).id}\n", encoding="utf-8-sig")

    result = _run(project, "--releases", ids)

    assert result.exit_code == 0, result.output
    assert transport_runs.ran == [_release(13).id]


def test_run_the_releases_of_a_submission(tmp_path, transport_runs):
    """A Slurm task reads the rows of its submission's releases, not releases.csv."""
    project = _project(tmp_path)
    rows = tmp_path / "releases.parquet"
    project._rows_of([_release(13).id]).to_parquet(rows, index=False)
    project.releases_path.write_text("not, a, releases file\n")

    result = _run(project, "--releases", rows)

    assert result.exit_code == 0, result.output
    assert transport_runs.ran == [_release(13).id]
    assert "Releases loaded: 1" in result.output


def test_run_one_task_of_a_job_array(tmp_path, transport_runs):
    project = _project(tmp_path, [_release(h) for h in (12, 13, 14)])

    result = _run(project, "--task", "1/2", "--backend", "local")

    assert result.exit_code == 0, result.output
    assert transport_runs.ran == [_release(13).id]
    assert "task 1 of 2  cpus=1" in result.output
    assert "Releases of this task: 1, 3, 5, ..." in result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--task", "two/four"], "--task takes I/N"),
        (["--task", "2/2"], "--task I/N needs 0 <= I < N"),
        (["--task", "0/2", "--backend", "slurm"], "drop --backend slurm"),
        (["--releases", "missing.txt"], "cannot read missing.txt"),
        (["--execution", "missing.yaml"], "cannot read missing.yaml"),
        (["--cpus", "0"], "execution.local.cpus: "),
    ],
)
def test_run_with_a_wrong_command_line_exits_2_and_runs_nothing(
    tmp_path, transport_runs, monkeypatch, args, message
):
    project = _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = _run(project, *args)

    assert result.exit_code == 2
    assert "Error: " in result.output
    assert message in result.output
    assert transport_runs.ran == []


def test_run_of_a_release_the_project_does_not_have_exits_2(tmp_path, transport_runs):
    project = _project(tmp_path)
    ids = tmp_path / "ids.txt"
    ids.write_text("nope\n")

    result = _run(project, "--releases", ids)

    assert result.exit_code == 2
    assert result.output.splitlines()[-1].startswith("Error: ")
    assert transport_runs.ran == []


def test_run_takes_its_execution_settings_from_a_file(tmp_path, calls):
    project = _project(tmp_path)
    settings = tmp_path / "execution.yaml"
    settings.write_text("local:\n  cpus: 3\ntimeout: 60\n")
    calls.states = ["complete"]

    result = _run(project, "--execution", settings)

    assert result.exit_code == 0, result.output
    [(_, options)] = calls
    assert options["execution"].local.cpus == 3
    assert options["execution"].timeout == 60
    assert "cpus=3" in result.output


@pytest.mark.parametrize(
    ("states", "code"),
    [
        (["complete", "complete"], 0),
        (["complete", "failed"], 1),
        (["failed", "interrupted"], 3),
        (["complete", "pending"], 3),
    ],
)
def test_run_exits_with_the_code_of_what_it_ran(tmp_path, calls, states, code):
    project = _project(tmp_path)
    calls.states = states
    ids = tmp_path / "ids.txt"
    ids.write_text(f"{_release(12).id}\n")

    result = _run(project, "--releases", ids)

    assert result.exit_code == code, result.output


def test_run_whose_worker_died_exits_3(tmp_path, monkeypatch):
    project = _project(tmp_path)

    def died(self, **kwargs):
        raise WorkerDied("a worker was killed")

    monkeypatch.setattr("runtraj.cli.Project.run", died)

    result = _run(project)

    assert result.exit_code == 3
    assert "runtraj run stopped: a worker was killed" in result.output


@pytest.mark.parametrize(
    ("error", "code", "said"),
    [
        (KeyboardInterrupt(), 3, "Interrupted."),
        (ConnectionResetError("reset"), 3, "runtraj exits with 3"),
        (RuntimeError("a bug"), 4, "runtraj exits with 4"),
    ],
)
def test_run_ends_an_error_it_did_not_expect_in_an_exit_code(
    tmp_path, monkeypatch, error, code, said
):
    project = _project(tmp_path)

    def raises(self, **kwargs):
        raise error

    monkeypatch.setattr("runtraj.cli.Project.run", raises)

    result = _run(project)

    assert result.exit_code == code
    assert said in result.output


def test_run_on_slurm_submits_and_waits(tmp_path, calls):
    project = _project(tmp_path)
    calls.states = ["complete", "complete"]

    result = _run(project, "--backend", "slurm", "--n-workers", "4", "--cpus", "2")

    assert result.exit_code == 0, result.output
    [(_, options)] = calls
    execution = options["execution"]
    assert execution.backend == "slurm"
    assert (execution.slurm.n_workers, execution.slurm.cpus) == (4, 2)
    assert "backend=slurm  tasks=40  cpus=2" in result.output  # ten per worker
    assert "Submitting to Slurm and waiting for the job to finish" in result.output


# ---------------------------------------------------------------------------
# submit command
# ---------------------------------------------------------------------------


def test_submit_sends_a_job_array_and_returns(tmp_path, calls):
    project = _project(tmp_path)

    result = _invoke("submit", "--n-workers", "3", project=project)

    assert result.exit_code == 0, result.output
    [(what, options)] = calls
    assert what == "submit"
    assert options["execution"].backend == "slurm"
    assert options["execution"].slurm.n_workers == 3
    assert options["compute_root"] is None
    assert "Compute root: each task's own $TMPDIR (or RUNTRAJ_COMPUTE_ROOT)" in (
        result.output
    )
    assert "Submitting to Slurm and returning" in result.output
    assert result.output.splitlines()[-1] == "Submitted job: 12345"


def test_submit_with_nothing_to_do_prints_the_status(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr("runtraj.cli.Project.submit", lambda self, **kwargs: None)

    result = _invoke("submit", project=project)

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1].startswith(f"Project: {project.directory}")
    assert "Submitted job" not in result.output


# ---------------------------------------------------------------------------
# Kubernetes: submit, and a pod's run of a project on a bucket
# ---------------------------------------------------------------------------


def _on_kubernetes(project: Project) -> Project:
    """Set *project*'s config.yaml to run on Kubernetes, and open it again."""
    path = project.directory / "config.yaml"
    settings = yaml.safe_load(path.read_text())
    settings["execution"] = {
        "backend": "kubernetes",
        "kubernetes": {"image": "registry.example.org/runtraj:1", "completions": 5},
    }
    path.write_text(yaml.safe_dump(settings))
    return Project(project.directory)


def test_submit_makes_a_kubernetes_job_when_config_yaml_says_so(tmp_path, calls):
    project = _on_kubernetes(_project(tmp_path))

    result = _invoke("submit", "--cpus", "2", project=project)

    assert result.exit_code == 0, result.output
    [(what, options)] = calls
    execution = options["execution"]
    assert (what, execution.backend) == ("submit", "kubernetes")
    assert execution.kubernetes.cpus == 2
    assert "backend=kubernetes  tasks=5  cpus=2" in result.output
    assert "Making a Kubernetes indexed Job and returning" in result.output


def test_submit_backend_is_slurm_or_kubernetes(tmp_path, calls):
    result = _invoke("submit", "--backend", "local", project=_project(tmp_path))
    assert result.exit_code == 2
    assert "slurm or kubernetes" in result.output


def test_a_pod_runs_a_project_whose_inputs_are_on_a_bucket(
    tmp_path, calls, monkeypatch
):
    from runtraj.execution.inputs import hash_inputs, push

    project = _project(tmp_path)
    url = "memory://cli-bucket/projects/proj"
    push(project.directory, url)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "pod-tmp"))

    result = _invoke(
        "run",
        url,
        "--task",
        "1/2",
        "--inputs-hash",
        hash_inputs(project.directory),
    )

    assert result.exit_code == 0, result.output
    [copy] = (tmp_path / "pod-tmp" / "runtraj-inputs").glob("*/proj")
    assert (copy / "releases.csv").exists()
    [(_, options)] = calls
    assert options["task"] == (1, 2)


@pytest.mark.parametrize("on_bucket", [True, False])
def test_a_pod_with_fetch_gets_the_met_locked_where_the_inputs_are(
    tmp_path, calls, monkeypatch, on_bucket
):
    from runtraj.execution.inputs import push

    project = _project(tmp_path)
    url = "memory://cli-fetch/projects/proj"
    push(project.directory, url)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "pod-tmp"))
    fetched = []
    monkeypatch.setattr(
        "runtraj.cli.fetch_shared",
        lambda opened, location: fetched.append((opened.directory, location)),
    )
    where = url if on_bucket else str(project.directory)

    result = _invoke("run", where, "--task", "0/2", "--fetch")

    assert result.exit_code == 0, result.output
    [(directory, location)] = fetched
    assert location == (url if on_bucket else str(project.directory))
    assert [what for what, _ in calls] == ["run"]  # the share runs after it


def test_a_pod_refuses_inputs_changed_after_submit(tmp_path, calls):
    project = _project(tmp_path)

    result = _run(project, "--task", "0/1", "--inputs-hash", "not-the-hash")

    assert result.exit_code == 2
    assert "changed after the Job was submitted" in result.output
    assert calls == []


def test_a_bucket_that_cannot_be_read_exits_with_3(tmp_path, calls, monkeypatch):
    def refused(url, directory):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr("runtraj.cli.pull", refused)

    result = _invoke("run", "s3://bucket/projects/proj", "--task", "0/1")

    assert result.exit_code == 3
    assert "connection refused" in result.output


def test_a_bucket_with_no_project_exits_with_2(tmp_path, calls):
    result = _invoke("run", "memory://no-such-bucket/proj", "--task", "0/1")
    assert result.exit_code == 2
    assert "no config.yaml" in result.output


JOB = {
    "name": "runtraj-proj-x7k2p",
    "state": "failed",
    "reason": "FailedIndexes",
    "created": "2026-10-29T16:00:00Z",
    "tasks": 5,
    "done": 3,
    "failed": 2,
    "failed_tasks": "1,4",
    "running": 0,
}


def test_status_lists_the_project_s_jobs_on_kubernetes(tmp_path, monkeypatch):
    project = _on_kubernetes(_project(tmp_path))
    monkeypatch.setattr(
        "runtraj.execution.kubernetes.project_jobs", lambda project, execution: [JOB]
    )

    result = _invoke("status", project=project)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[1:3] == [
        "Jobs:",
        "  runtraj-proj-x7k2p  failed (FailedIndexes)  3 of 5 tasks done, 0 running, "
        "2 failed (tasks 1,4)  started 2026-10-29T16:00:00Z",
    ]


def test_status_as_json_has_the_jobs_on_kubernetes(tmp_path, monkeypatch):
    project = _on_kubernetes(_project(tmp_path))
    monkeypatch.setattr(
        "runtraj.execution.kubernetes.project_jobs", lambda project, execution: [JOB]
    )

    result = _invoke("status", "--json", project=project)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["jobs"] == [JOB]


def test_status_still_counts_when_the_jobs_cannot_be_listed(tmp_path, monkeypatch):
    project = _on_kubernetes(_project(tmp_path))

    def refuse(project, execution):
        raise FileNotFoundError("kubectl is not installed, or not on PATH.")

    monkeypatch.setattr("runtraj.execution.kubernetes.project_jobs", refuse)

    result = _invoke("status", project=project)

    assert result.exit_code == 0, result.output
    assert "total=2" in result.output
    assert "Jobs: not listed. kubectl is not installed" in result.output


def test_status_lists_no_jobs_on_another_backend(tmp_path, monkeypatch):
    def unexpected(project, execution):
        raise AssertionError("kubectl was asked")

    monkeypatch.setattr("runtraj.execution.kubernetes.project_jobs", unexpected)

    result = _invoke("status", "--json", project=_project(tmp_path))

    assert result.exit_code == 0, result.output
    assert "jobs" not in json.loads(result.output)


# ---------------------------------------------------------------------------
# Kubernetes: schedule, and each hour's tick
# ---------------------------------------------------------------------------

TOWER = "longitude,latitude,altitude\n-112.0,40.5,10\n"


def test_a_tick_adds_the_due_hours_on_the_bucket_and_runs_them_here(
    tmp_path, calls, monkeypatch
):
    from runtraj.execution import schedule
    from runtraj.execution.inputs import push

    project = _project(tmp_path)
    (project.directory / "sites.csv").write_text(TOWER)
    url = "memory://cli-tick/projects/proj"
    push(project.directory, url)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "pod-tmp"))

    result = _invoke("tick", url, "--now", "2023-01-02T20:15", "--cpus", "2")

    assert result.exit_code == 0, result.output
    [(what, options)] = calls
    hours = schedule.due_hours(
        dt.datetime(2023, 1, 2, 20, 15), since=NOON.replace(hour=13)
    )
    assert what == "run"
    assert options["releases"][0].startswith(f"{hours[0]:%Y%m%d%H%M}_")
    assert len(options["releases"]) == len(hours)
    assert options["execution"].backend == "local"
    assert options["execution"].cpus == 2
    [copy] = (tmp_path / "pod-tmp" / "runtraj-inputs").glob("*/proj")
    rows = Project(copy).releases
    assert len(rows) == 2 + len(hours)


def test_two_urls_of_one_name_are_copied_to_two_folders(tmp_path, monkeypatch):
    """Two tasks on one machine, or two projects named alike, never read each other's inputs."""
    from runtraj.cli import _local_inputs
    from runtraj.execution.inputs import push

    project = _project(tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    urls = [f"memory://cli-{bucket}/projects/proj" for bucket in ("a", "b")]
    for url in urls:
        push(project.directory, url)

    first, second = (Path(_local_inputs(url)) for url in urls)

    assert first != second
    assert first.name == second.name == "proj"  # the project's name
    assert (first / "config.yaml").exists() and (second / "config.yaml").exists()


def test_a_tick_refuses_a_now_later_than_the_clock(tmp_path, calls):
    project = _project(tmp_path)
    (project.directory / "sites.csv").write_text(TOWER)

    result = _invoke("tick", "--now", "2999-01-01T00:00", project=project)

    assert result.exit_code == 2
    assert "later than the clock" in result.output
    assert calls == []


def test_a_tick_names_the_line_of_a_site_it_refuses(tmp_path, calls):
    project = _project(tmp_path)
    (project.directory / "sites.csv").write_text(
        "longitude,latitude,altitude\n-111.85,95,5\n"
    )

    result = _invoke("tick", project=project)

    assert result.exit_code == 2
    assert "sites.csv line 2: latitude" in result.output
    assert calls == []


def test_a_tick_that_cannot_lock_the_releases_exits_with_3(
    tmp_path, calls, monkeypatch
):
    from runtraj.execution import inputs

    project = _project(tmp_path)
    (project.directory / "sites.csv").write_text(TOWER)

    def refuse(*args):
        raise PermissionError("access denied")

    monkeypatch.setattr(inputs, "_acquire", refuse)

    result = _invoke("tick", project=project)

    assert result.exit_code == 3
    assert "cannot lock releases.csv" in result.output
    assert "access denied" in result.output
    assert calls == []


@pytest.mark.parametrize("command", ["schedule", "tick"])
def test_a_delay_beyond_a_week_is_refused(tmp_path, command):
    project = _on_kubernetes(_project(tmp_path))

    result = _invoke(command, "--delay", "100000", project=project)

    assert result.exit_code == 2
    # CI colors the usage error and draws it in a box: keep only its words.
    words = re.sub(r"[│╭╮╰╯─]", " ", re.sub(r"\x1b\[[0-9;]*m", "", result.output))
    assert "--delay" in words and "168" in words


def test_a_kubernetes_block_with_a_bad_setting_exits_2_naming_it(tmp_path):
    project = _on_kubernetes(_project(tmp_path))
    path = project.directory / "config.yaml"
    settings = yaml.safe_load(path.read_text())
    settings["execution"]["kubernetes"]["volumes"] = [
        {"claim_name": "met", "mount_path": "/scratch"}
    ]
    path.write_text(yaml.safe_dump(settings))

    result = _invoke("status", project=project)

    assert result.exit_code == 2
    assert "mount_path" in result.output and "/scratch" in result.output
    assert "Traceback" not in result.output


def test_schedule_makes_the_cron_job_with_the_kubernetes_block(tmp_path, monkeypatch):
    project = _on_kubernetes(_project(tmp_path))
    made = []

    def schedule_job(opened, execution, *, delay):
        made.append((opened.directory, execution, delay))
        return "runtraj-proj-12345678"

    monkeypatch.setattr("runtraj.cli.schedule_job", schedule_job)

    result = _invoke("schedule", "--delay", "6", project=project)

    assert result.exit_code == 0, result.output
    assert "Scheduled: runtraj-proj-12345678" in result.output
    [(directory, execution, delay)] = made
    assert (directory, execution.backend, delay) == (project.directory, "kubernetes", 6)


def test_schedule_needs_an_image(tmp_path):
    result = _invoke("schedule", project=_project(tmp_path))
    assert result.exit_code == 2
    assert "kubernetes.image is required" in result.output


def test_schedule_needs_sites(tmp_path):
    result = _invoke("schedule", project=_on_kubernetes(_project(tmp_path)))
    assert result.exit_code == 2
    assert "has no sites.csv" in result.output


# ---------------------------------------------------------------------------
# An error the command did not expect: 3 to retry, 4 not to
# ---------------------------------------------------------------------------


def _error_named(name, *bases):
    """Return an exception of a class called *name*, as a cloud library would raise it."""
    return type(name, bases or (Exception,), {})("it failed")


def _client_error(status, code="Error"):
    """Return what botocore raises for a response with this status and error code."""
    error = _error_named("ClientError")
    error.response = {
        "ResponseMetadata": {"HTTPStatusCode": status},
        "Error": {"Code": code},
    }
    return error


#: Base classes the cloud libraries' errors share, named as botocore names them.
_BOTOCORE_CONNECTION_ERROR = type("ConnectionError", (Exception,), {})
_BOTOCORE_HTTP_CLIENT_ERROR = type("HTTPClientError", (Exception,), {})


def _with_status(attribute, status):
    """Return an error that carries an HTTP status as aiohttp or gcsfs set it."""
    error = RuntimeError("the service answered")
    setattr(error, attribute, status)
    return error


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        # Connection and I/O errors: run it again.
        (ConnectionRefusedError("refused"), True),
        (TimeoutError("slow"), True),
        (OSError(5, "Input/output error"), True),
        (OSError(errno.ESTALE, "Stale file handle"), True),
        # Cloud libraries' errors that are not OSError, matched by class name.
        (_error_named("EndpointConnectionError", _BOTOCORE_CONNECTION_ERROR), True),
        (_error_named("ConnectTimeoutError", _BOTOCORE_CONNECTION_ERROR), True),
        (_error_named("ReadTimeoutError", _BOTOCORE_HTTP_CLIENT_ERROR), True),
        # aiohttp's ServerDisconnectedError is a ClientConnectionError.
        (
            _error_named(
                "ServerDisconnectedError",
                type("ClientConnectionError", (Exception,), {}),
            ),
            True,
        ),
        (_error_named("ClientConnectionError"), True),
        (_client_error(503, "SlowDown"), True),
        (_client_error(500, "InternalError"), True),
        (_client_error(429), True),
        # The status of a busy service, wherever the library keeps it.
        (_with_status("status", 503), True),
        (_with_status("code", 502), True),
        (_with_status("status", 404), False),
        (_with_status("code", "not a status"), False),
        # Not transient: the path or the credentials are wrong, or it is a bug.
        (_client_error(403, "AccessDenied"), False),
        (_client_error(404, "NoSuchKey"), False),
        (_client_error(None, "NoSuchKey"), False),
        (FileNotFoundError("no such file"), False),
        (PermissionError("denied"), False),
        (IsADirectoryError("a directory"), False),
        (RuntimeError("a bug"), False),
        (KeyError("k"), False),
        (ValueError("bad"), False),
        (_error_named("AnErrorNoRuleNames"), False),
    ],
)
def test_is_transient_sorts_retry_from_bug(error, transient):
    assert is_transient(error) is transient


def _as_s3fs_raises(error_errno, status, code):
    """
    Return what s3fs raises for a botocore ``ClientError``: an ``OSError`` with its errno, and the ``ClientError`` as ``__cause__``.

    ``s3fs.errors.translate_boto_error`` does this for every code it knows.
    """
    wrapped = OSError(error_errno, "the store answered")
    wrapped.__cause__ = _client_error(status, code)
    return wrapped


def _raised_from(outer, inner):
    """Return *outer* with *inner* as its ``__cause__``, as ``raise outer from inner`` does."""
    outer.__cause__ = inner
    return outer


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        # s3fs turns a permanent S3 answer into a plain OSError: a retry meets it again.
        (_as_s3fs_raises(_EREMCHG, 301, "PermanentRedirect"), False),
        (_as_s3fs_raises(errno.EINVAL, 400, "AuthorizationHeaderMalformed"), False),
        (_as_s3fs_raises(errno.EINVAL, 400, "InvalidBucketName"), False),
        (_as_s3fs_raises(errno.EINVAL, 400, "InvalidArgument"), False),
        (_as_s3fs_raises(errno.EINVAL, 400, "400"), False),
        (_as_s3fs_raises(errno.EPERM, 405, "MethodNotAllowed"), False),
        (_as_s3fs_raises(errno.EIO, 418, "Teapot"), False),
        (
            _raised_from(PermissionError("denied"), _client_error(403, "AccessDenied")),
            False,
        ),
        (
            _raised_from(FileNotFoundError("gone"), _client_error(404, "NoSuchBucket")),
            False,
        ),
        # ... and a busy one, a retry may get past.
        (_as_s3fs_raises(errno.EBUSY, 503, "SlowDown"), True),
        (_as_s3fs_raises(errno.EBUSY, 503, "ServiceUnavailable"), True),
        (_as_s3fs_raises(errno.EBUSY, 409, "OperationAborted"), True),
        (_as_s3fs_raises(_EREMOTEIO, 500, "InternalError"), True),
        (_as_s3fs_raises(errno.EIO, 502, "BadGateway"), True),
        (
            _raised_from(TimeoutError("slow"), _client_error(400, "RequestTimeout")),
            True,
        ),
        # urllib raises an OSError that holds an HTTP status.
        (urllib.error.HTTPError("u", 404, "not found", {}, io.BytesIO()), False),
        (urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO()), True),
        # An OSError nothing explains is read by its errno.
        (OSError(errno.EIO, "Input/output error"), True),
        (OSError(errno.ESTALE, "Stale file handle"), True),
        (OSError(errno.ETIMEDOUT, "timed out"), True),
        (socket.gaierror(socket.EAI_AGAIN, "try again"), True),
        (OSError(errno.EINVAL, "invalid"), False),
        (OSError(errno.ENOSPC, "no space left"), False),
        (OSError(errno.EROFS, "read-only"), False),
        (OSError("an OSError with no errno"), False),
        # The chain is read for a bug raised while handling or from a connection error.
        (_raised_from(RuntimeError("wrapped"), ConnectionResetError("reset")), True),
        (_raised_from(RuntimeError("wrapped"), _client_error(403)), False),
    ],
)
def test_is_transient_reads_the_chain_and_the_status(error, transient):
    assert is_transient(error) is transient


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        # FTP: a 4xx reply is transient by the standard, a 5xx is not.
        (ftplib.error_temp("421 There are too many connected users"), True),
        (ftplib.error_temp("425 Can't open data connection"), True),
        (ftplib.error_perm("550 No such file"), False),
        (ftplib.error_perm("530 Login incorrect"), False),
        (ftplib.error_reply("unexpected reply"), False),
        (ftplib.error_proto("no FTP reply"), False),
        # fsspec's HTTP filesystem raises FileNotFoundError for every failure,
        # from the error that happened: that one decides.
        (
            _raised_from(FileNotFoundError("http://x/f"), _with_status("status", 503)),
            True,
        ),
        (
            _raised_from(FileNotFoundError("http://x/f"), _with_status("status", 429)),
            True,
        ),
        (
            _raised_from(FileNotFoundError("http://x/f"), _with_status("status", 404)),
            False,
        ),
        (
            _raised_from(FileNotFoundError("http://x/f"), ConnectionRefusedError()),
            True,
        ),
        (_raised_from(FileNotFoundError("http://x/f"), TimeoutError()), True),
        (_raised_from(FileNotFoundError("http://x/f"), KeyError("k")), False),
        (
            _raised_from(
                FileNotFoundError("http://x/f"),
                _error_named(
                    "ClientOSError", type("ClientConnectionError", (Exception,), {})
                ),
            ),
            True,
        ),
        (_raised_from(PermissionError("denied"), _with_status("status", 503)), True),
        (FileNotFoundError("no cause"), False),
    ],
)
def test_is_transient_ftp_replies_and_wrapped_http_errors(error, transient):
    assert is_transient(error) is transient


def test_an_eoferror_from_ftplib_is_transient_and_one_from_other_code_is_not():
    # ftplib's own read of a closed connection, with no network
    ftp = ftplib.FTP()
    ftp.file = io.BytesIO(b"")
    with pytest.raises(EOFError) as dropped:
        ftp.getline()
    assert is_transient(dropped.value)

    # a bare EOFError, as arlmet raises for an ARL file that is cut short
    with pytest.raises(EOFError) as cut_short:
        raise EOFError("file ends in the middle of a record")
    assert not is_transient(cut_short.value)
    assert not is_transient(EOFError("never raised"))


def test_an_error_raised_while_handling_a_connection_error_is_read_with_it():
    try:
        try:
            raise ConnectionRefusedError("refused")
        except ConnectionRefusedError:
            raise RuntimeError("the handler failed") from None
    except RuntimeError as suppressed:
        assert not is_transient(suppressed)
    try:
        try:
            raise ConnectionRefusedError("refused")
        except ConnectionRefusedError:
            raise RuntimeError("the handler failed")  # noqa: B904
    except RuntimeError as handling:
        assert is_transient(handling)


def test_s3fs_own_translation_is_sorted_by_status():
    """The real s3fs: every permanent code is 4 and a busy one 3, as the stand-ins above."""
    translate = pytest.importorskip("s3fs.errors").translate_boto_error
    for name, status, transient in [
        ("PermanentRedirect", 301, False),
        ("AuthorizationHeaderMalformed", 400, False),
        ("InvalidBucketName", 400, False),
        ("InvalidArgument", 400, False),
        ("AccessDenied", 403, False),
        ("NoSuchBucket", 404, False),
        ("SlowDown", 503, True),
        ("InternalError", 500, True),
        ("SomethingNew", 500, True),
        ("SomethingNew", 418, False),
    ]:
        error = translate(_client_error(status, name))
        assert is_transient(error) is transient, (name, status)


def test_real_botocore_errors_are_sorted_by_their_base_class():
    """Every network error botocore raises is 3 through a base class; these are the real ones."""
    botocore = pytest.importorskip("botocore.exceptions")
    for error in (
        botocore.EndpointConnectionError(endpoint_url="u"),
        botocore.ProxyConnectionError(proxy_url="u"),
        botocore.ConnectTimeoutError(endpoint_url="u"),
        botocore.SSLError(endpoint_url="u", error="e"),
        botocore.ReadTimeoutError(endpoint_url="u"),
        botocore.ConnectionClosedError(endpoint_url="u"),
        botocore.IncompleteReadError(actual_bytes=1, expected_bytes=2),
    ):
        assert is_transient(error), type(error).__name__
    assert not is_transient(botocore.NoCredentialsError())


def test_real_aiohttp_errors_are_sorted_by_their_base_class():
    """A dropped connection or a body cut off is 3, whatever ``code`` the cause holds."""
    aiohttp = pytest.importorskip("aiohttp")
    for error in (
        aiohttp.ServerDisconnectedError(),
        aiohttp.ClientOSError(errno.ECONNRESET, "reset"),
        aiohttp.ClientPayloadError("the body was cut off"),
    ):
        assert is_transient(error), type(error).__name__


def test_a_gcsfs_bad_gateway_is_a_connection_error():
    """A proxy's 502 reaches gcsfs through ``requests``, which raises ``ProxyError``."""
    requests = pytest.importorskip("requests.exceptions")
    assert is_transient(requests.ProxyError("502 Bad Gateway"))


def test_s3fs_busy_errno_decides_for_a_read_with_no_error_code():
    """A read's HEAD has no body, so botocore's code is the status; s3fs's errno still says busy."""
    translate = pytest.importorskip("s3fs.errors").translate_boto_error
    assert is_transient(translate(_client_error(409, "409")))


def test_a_cloud_error_is_sorted_with_the_libraries_not_imported():
    """A botocore error is 3 and a denied one 4, as for the stand-ins above."""
    botocore = pytest.importorskip("botocore.exceptions")
    refused = botocore.EndpointConnectionError(endpoint_url="http://127.0.0.1:9")
    denied = botocore.ClientError(
        {
            "Error": {"Code": "AccessDenied"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "ListObjectsV2",
    )
    assert is_transient(refused)
    assert is_transient(botocore.ReadTimeoutError(endpoint_url="u"))
    assert not is_transient(denied)


@not_on_windows("a closed pipe raises BrokenPipeError differently on Windows")
def test_status_into_a_closed_pipe_is_quiet_and_exits_1(tmp_path):
    """A real pipe whose reader has gone: no traceback, and not Python's 120 at shutdown."""
    _write_minimal_config(tmp_path)
    child = subprocess.Popen(
        [sys.executable, "-m", "runtraj", "status", str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    child.stdout.close()
    _, stderr = child.communicate(timeout=300)

    assert child.returncode == 1, stderr
    assert "Traceback" not in stderr
    assert "runtraj exits with" not in stderr


def test_an_unreachable_object_store_exits_3_not_1(tmp_path):
    """`runtraj run` with an output bucket nothing answers."""
    pytest.importorskip("s3fs")
    _write_minimal_config(tmp_path)
    env = {
        **os.environ,
        "AWS_ENDPOINT_URL": "http://127.0.0.1:9",
        "AWS_ACCESS_KEY_ID": "x",
        "AWS_SECRET_ACCESS_KEY": "x",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "runtraj",
            "run",
            str(tmp_path),
            "--output",
            "s3://runtraj/output",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )

    assert done.returncode == 3, done.stderr
    assert "runtraj exits with 3" in done.stderr


# ---------------------------------------------------------------------------
# init command
# ---------------------------------------------------------------------------

"""Tests for runtraj.execution.backends: the built-in backends, and one named by its import path."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from runtraj import Project
from runtraj.cli import app
from runtraj.exceptions import MissingClass
from runtraj.execution import Backend, ExecutionConfig, get_backend, run, submit
from runtraj.execution.backends import BACKENDS
from runtraj.execution.config import BUILT_IN_BLOCKS

from ..fixtures import backends as toy
from ..fixtures.slurm import hourly_project

QUEUE = "tests.fixtures.backends.QueueBackend"
WAITING = "tests.fixtures.backends.WaitingQueueBackend"


@pytest.fixture(autouse=True)
def _no_calls():
    """Start each test with no recorded call of the toy backends."""
    toy.CALLS.clear()


@pytest.mark.parametrize("name", list(BACKENDS))
def test_a_built_in_backend_reads_the_block_of_its_name(name):
    backend = get_backend(name)
    assert isinstance(backend, Backend)
    assert backend.name == name
    assert backend.config_class is BUILT_IN_BLOCKS[name]
    assert ExecutionConfig.model_fields[name].annotation is backend.config_class


def test_an_unknown_backend_name_says_how_to_name_one():
    with pytest.raises(ValueError, match=r"Unknown backend 'lsf'.*import path"):
        get_backend("lsf")


def test_a_class_that_is_not_a_backend_is_refused():
    with pytest.raises(TypeError, match="subclasses runtraj.execution.Backend"):
        get_backend("tests.fixtures.backends.NotABackend")


def test_a_backend_that_is_not_installed_names_its_package():
    with pytest.raises(MissingClass, match="nopkg is not installed"):
        ExecutionConfig(backend="nopkg.backends.Gone")


def test_a_backend_by_import_path_reads_the_block_of_its_name():
    config = ExecutionConfig(backend=QUEUE, myqueue={"queue": "long", "cpus": 3})

    assert config.block.queue == "long"
    assert (config.cpus, config.block.tasks) == (3, 4)


def test_its_block_is_kept_when_the_settings_are_written_and_read_again():
    config = ExecutionConfig(backend=QUEUE, myqueue={"queue": "long", "cpus": 3})

    dumped = config.model_dump(exclude_unset=True)
    assert dumped["myqueue"]["queue"] == "long"
    assert ExecutionConfig.model_validate(dumped) == config


def test_its_block_is_checked_by_its_own_rules():
    with pytest.raises(ValueError, match="myqueue.queue is required"):
        ExecutionConfig(backend=QUEUE)
    with pytest.raises(ValueError, match=r"cpuss\s+Extra inputs"):
        ExecutionConfig(backend=QUEUE, myqueue={"queue": "q", "cpuss": 2})


def test_a_setting_of_its_block_outside_it_names_the_block():
    with pytest.raises(ValueError, match=r"Write it under execution: myqueue:"):
        ExecutionConfig(backend=QUEUE, myqueue={"queue": "q"}, pods=2)


def test_its_block_is_refused_when_another_backend_is_named():
    with pytest.raises(ValueError, match="'myqueue' is not a setting of execution"):
        ExecutionConfig(myqueue={"queue": "q"})


def test_a_block_named_as_a_setting_of_execution_is_refused():
    with pytest.raises(ValueError, match="names its block 'timeout'"):
        ExecutionConfig(backend="tests.fixtures.backends.TimeoutBackend")


def test_submit_hands_the_unfinished_releases_to_the_backend(tmp_path):
    project = hourly_project(tmp_path, 3)
    execution = ExecutionConfig(backend=QUEUE, myqueue={"queue": "q"})

    assert submit(project, execution=execution) == "queue-7"

    [(method, releases, given)] = toy.CALLS
    assert (method, given) == ("submit", execution)
    assert releases == list(project.releases["release"])


def test_run_refuses_a_backend_it_cannot_wait_for(tmp_path):
    project = hourly_project(tmp_path, 1)

    with pytest.raises(
        ValueError, match="does not wait for the tasks of backend 'myqueue'"
    ):
        run(project, execution=ExecutionConfig(backend=QUEUE, myqueue={"queue": "q"}))
    assert toy.CALLS == []


def test_run_returns_what_a_waiting_backend_returns(tmp_path):
    project = hourly_project(tmp_path, 2)

    table = run(
        project, execution=ExecutionConfig(backend=WAITING, myqueue={"queue": "q"})
    )

    assert [method for method, *_ in toy.CALLS] == ["run"]
    assert table["state"].tolist() == ["complete", "complete"]


def _on_queue(project: Project) -> Project:
    """Set *project*'s execution to the toy queue backend, and open it again."""
    import yaml

    path = project.directory / "config.yaml"
    settings = yaml.safe_load(path.read_text(encoding="utf-8"))
    settings["execution"] = {"backend": QUEUE, "myqueue": {"queue": "q"}}
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    return Project(project.directory)


def test_runtraj_submit_uses_the_backend_config_yaml_names(tmp_path):
    project = _on_queue(hourly_project(tmp_path, 2))

    result = CliRunner().invoke(app, ["submit", str(project.directory), "--cpus", "2"])

    assert result.exit_code == 0, result.output
    assert f"backend={QUEUE}  tasks=4  cpus=2" in result.output
    assert "Submitting to myqueue and returning" in result.output
    assert "Submitted job: queue-7" in result.output
    [(_, _, execution)] = toy.CALLS
    assert execution.block.cpus == 2


def test_runtraj_status_lists_the_backend_s_jobs(tmp_path):
    project = _on_queue(hourly_project(tmp_path, 1))

    result = CliRunner().invoke(app, ["status", str(project.directory)])

    assert result.exit_code == 0, result.output
    assert "  queue-7  running  1 of 4 tasks done, 3 running" in result.output


def test_runtraj_submit_refuses_the_local_backend(tmp_path):
    project = hourly_project(tmp_path, 1)

    result = CliRunner().invoke(
        app, ["submit", str(project.directory), "--backend", "local"]
    )

    assert result.exit_code == 2
    assert "--backend for submit is a scheduler" in result.output


def test_runtraj_run_names_a_backend_it_cannot_find(tmp_path):
    project = hourly_project(tmp_path, 1)

    result = CliRunner().invoke(
        app, ["run", str(project.directory), "--backend", "lsf", "--cpus", "2"]
    )

    assert result.exit_code == 2
    assert "Unknown backend 'lsf'" in result.output

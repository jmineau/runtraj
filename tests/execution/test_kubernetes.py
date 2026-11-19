"""Tests for the Kubernetes backend: its config block, the indexed Job, and copying the inputs to a bucket."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import fsspec
import pytest
import yaml
from pydantic import ValidationError

from runtraj.execution import kubernetes, runner
from runtraj.execution.config import ExecutionConfig, KubernetesConfig
from runtraj.execution.inputs import hash_inputs, pull, push
from runtraj.project import Project
from runtraj.releases import PointRelease

from ..fixtures.factories import make_project_config

IMAGE = "registry.example.org/runtraj-hysplit:1"


def _project(tmp_path: Path, n: int = 3, output: str | None = None) -> Project:
    """A project of *n* hourly releases, its output in ``tmp_path / "out"`` unless given."""
    config = make_project_config(tmp_path)
    releases = [
        PointRelease(
            time=dt.datetime(2023, 1, 1, h),
            longitude=-111.85,
            latitude=40.77,
            altitude=5.0,
        )
        for h in range(n)
    ]
    project = Project.init(tmp_path / "hourly", config=config, releases=releases)
    return Project(project.directory, output=output or str(tmp_path / "out"))


def _execution(**kube) -> ExecutionConfig:
    return ExecutionConfig(backend="kubernetes", kubernetes={"image": IMAGE, **kube})


# ---------------------------------------------------------------------------
# The kubernetes block
# ---------------------------------------------------------------------------


def test_memory_takes_kubernetes_units():
    assert KubernetesConfig(memory="8Gi").memory == "8Gi"
    assert KubernetesConfig(memory="512Mi").memory == "512Mi"


@pytest.mark.parametrize(("memory", "hint"), [("8G", "8Gi"), ("4000m", "4000Mi")])
def test_memory_in_other_units_is_refused_with_the_size_meant(memory, hint):
    with pytest.raises(ValidationError, match=f"Did you mean {hint}"):
        KubernetesConfig(memory=memory)


def test_a_kubernetes_run_needs_an_image():
    with pytest.raises(ValidationError, match="kubernetes.image is required"):
        ExecutionConfig(backend="kubernetes")
    # Only the block backend names is read.
    assert ExecutionConfig(backend="local").kubernetes.image is None


def test_cpus_and_fetch_come_from_the_kubernetes_block():
    execution = _execution(cpus=4)
    assert execution.cpus == 4
    assert execution.fetch is False


def test_a_setting_beside_backend_names_the_kubernetes_block():
    with pytest.raises(ValidationError, match="execution: kubernetes:"):
        ExecutionConfig.model_validate({"backend": "kubernetes", "completions": 4})


def test_a_url_with_one_slash_is_refused():
    with pytest.raises(ValidationError, match="did you mean 's3://bucket/p'"):
        KubernetesConfig(inputs="s3:/bucket/p")


def test_a_volume_path_must_be_absolute():
    with pytest.raises(ValidationError):
        KubernetesConfig(volumes=[{"claim_name": "met", "mount_path": "met"}])


# ---------------------------------------------------------------------------
# The Job
# ---------------------------------------------------------------------------


def test_the_job_runs_one_share_per_pod_with_the_block_s_resources():
    execution = _execution(
        cpus=4,
        memory="8Gi",
        parallelism=50,
        namespace="runs",
        secrets=["bucket-keys"],
        service_account="runner",
        active_deadline_seconds=3600,
        volumes=[{"claim_name": "hrrr-arl", "mount_path": "/met", "read_only": True}],
    )
    job = kubernetes.job_manifest(
        "My_Project",
        "s3://bucket/my_project",
        execution,
        20,
        output="s3://bucket/out",
        input_hash="ab12",
    )
    spec = job["spec"]
    assert job["metadata"]["generateName"] == "runtraj-my-project-"
    assert job["metadata"]["namespace"] == "runs"
    assert job["metadata"]["labels"][kubernetes.PROJECT_LABEL] == "my-project"
    assert (spec["completionMode"], spec["completions"]) == ("Indexed", 20)
    assert spec["parallelism"] == 20  # never more than the tasks
    assert spec["activeDeadlineSeconds"] == 3600
    pod = spec["template"]["spec"]
    [container] = pod["containers"]
    assert container["args"] == [
        "runtraj",
        "run",
        "s3://bucket/my_project",
        "--task",
        "$(JOB_COMPLETION_INDEX)/20",
        "--cpus",
        "4",
        "--output",
        "s3://bucket/out",
        "--inputs-hash",
        "ab12",
    ]
    assert container["resources"] == {
        "requests": {"cpu": "4", "memory": "8Gi"},
        "limits": {"memory": "8Gi"},
    }
    assert {"name": "TMPDIR", "value": kubernetes.SCRATCH} in container["env"]
    assert container["envFrom"] == [{"secretRef": {"name": "bucket-keys"}}]
    assert {"name": "volume-0", "mountPath": "/met", "readOnly": True} in container[
        "volumeMounts"
    ]
    assert {
        "name": "volume-0",
        "persistentVolumeClaim": {"claimName": "hrrr-arl"},
    } in pod["volumes"]
    assert pod["serviceAccountName"] == "runner"
    assert pod["securityContext"]["fsGroup"] == kubernetes.FS_GROUP
    assert "imagePullPolicy" not in container


def test_the_job_retries_a_task_only_on_exit_3_or_a_disruption():
    job = kubernetes.job_manifest(
        "p", "/project", _execution(), 2, output="/out", input_hash="h"
    )
    spec = job["spec"]
    actions = {
        tuple(rule["onExitCodes"]["values"]): rule["action"]
        for rule in spec["podFailurePolicy"]["rules"]
        if "onExitCodes" in rule
    }
    assert actions == {(2,): "FailJob", (1,): "FailIndex", (4,): "FailIndex"}
    assert spec["backoffLimitPerIndex"] == kubernetes.RETRIES
    ignored = [r for r in spec["podFailurePolicy"]["rules"] if r["action"] == "Ignore"]
    assert ignored[0]["onPodConditions"][0]["type"] == "DisruptionTarget"


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


@pytest.fixture
def kubectl(monkeypatch):
    """Record the manifests given to ``kubectl create``; ``answer`` is what it returns."""
    created: list[dict] = []
    state = SimpleNamespace(
        created=created,
        answer=SimpleNamespace(
            returncode=0, stdout="job.batch/runtraj-hourly-x7k2p\n", stderr=""
        ),
    )

    def fake_run(args, *, input, **kwargs):
        assert args[1:] == ["create", "-f", "-", "-o", "name"]
        created.append(yaml.safe_load(input))
        return state.answer

    monkeypatch.setattr(kubernetes.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(kubernetes.subprocess, "run", fake_run)
    return state


def test_submit_makes_a_job_for_a_project_on_a_shared_volume(tmp_path, kubectl):
    project = _project(tmp_path, 3)

    name = runner.submit(project, execution=_execution(completions=8))

    assert name == "runtraj-hourly-x7k2p"
    [job] = kubectl.created
    args = job["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args[2] == str(project.directory.resolve())
    assert job["spec"]["completions"] == 3  # no more tasks than releases
    assert args[args.index("--inputs-hash") + 1] == hash_inputs(project.directory)
    [folder] = (project.directory / "_kubernetes").iterdir()
    assert yaml.safe_load((folder / "job.yaml").read_text()) == job
    assert (folder / "job.name").read_text() == "runtraj-hourly-x7k2p\n"


def test_submit_copies_the_inputs_to_the_bucket_the_pods_open(tmp_path, kubectl):
    project = _project(tmp_path, 2, output="memory://submit-bucket/out")
    url = "memory://submit-bucket/projects/hourly"

    runner.submit(project, execution=_execution(inputs=url))

    fs = fsspec.filesystem("memory")
    assert fs.isfile("/submit-bucket/projects/hourly/releases.csv")
    [job] = kubectl.created
    args = job["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args[2] == url
    assert args[args.index("--output") + 1] == "memory://submit-bucket/out"


def test_submit_refuses_inputs_on_a_bucket_with_results_on_a_pod_s_disk(
    tmp_path, kubectl
):
    project = _project(tmp_path, 1)
    with pytest.raises(ValueError, match="each pod would write its results"):
        runner.submit(project, execution=_execution(inputs="memory://b/p"))
    assert kubectl.created == []


def test_submit_says_why_kubectl_refused(tmp_path, kubectl):
    kubectl.answer = SimpleNamespace(returncode=1, stdout="", stderr="forbidden: quota")
    with pytest.raises(RuntimeError, match="forbidden: quota"):
        runner.submit(_project(tmp_path, 1), execution=_execution())


def test_submit_without_kubectl_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(kubernetes.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="kubectl is not installed"):
        runner.submit(_project(tmp_path, 1), execution=_execution())


@pytest.mark.parametrize(
    "options", [{"releases": ["anything"]}, {"skip_existing": False}]
)
def test_a_kubernetes_job_takes_no_list_of_releases_and_no_no_skip(
    tmp_path, kubectl, options
):
    with pytest.raises(ValueError, match="every unfinished release"):
        runner.submit(_project(tmp_path, 1), execution=_execution(), **options)


def test_run_does_not_wait_for_a_kubernetes_job(tmp_path):
    with pytest.raises(ValueError, match="Use submit"):
        runner.run(_project(tmp_path, 1), execution=_execution())


# ---------------------------------------------------------------------------
# The inputs on a bucket
# ---------------------------------------------------------------------------


def test_inputs_go_to_a_bucket_and_back(tmp_path):
    project = _project(tmp_path, 2)
    tables = project.directory / "tables"
    tables.mkdir()
    (tables / "sites.parquet").write_bytes(b"table")
    url = "memory://roundtrip/projects/hourly"

    copied = push(project.directory, url)
    back = pull(url, tmp_path / "pod" / "hourly")

    assert copied == ["config.yaml", "releases.csv", "tables/sites.parquet"]
    assert (back / "tables" / "sites.parquet").read_bytes() == b"table"
    assert hash_inputs(back) == hash_inputs(project.directory)


def test_the_hash_changes_with_the_releases(tmp_path):
    project = _project(tmp_path, 2)
    before = hash_inputs(project.directory)
    with (project.directory / "releases.csv").open("a") as file:
        file.write("\n")
    assert hash_inputs(project.directory) != before


def test_pulling_where_there_is_no_project_says_so(tmp_path):
    with pytest.raises(FileNotFoundError, match="no config.yaml"):
        pull("memory://empty-bucket/nothing", tmp_path / "pod")


def test_a_bucket_that_cannot_be_read_is_an_os_error(tmp_path, monkeypatch):
    """A cloud library's error is an OSError, which the CLI turns into exit 3, not 2."""

    class Refused(Exception):
        pass

    def info(self, path, **kwargs):
        raise Refused("connection refused")

    monkeypatch.setattr(fsspec.implementations.memory.MemoryFileSystem, "info", info)
    with pytest.raises(OSError, match="connection refused"):
        pull("memory://down/p", tmp_path / "pod")

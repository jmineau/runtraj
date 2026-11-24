"""Tests for the Kubernetes backend: its config block, the indexed Job, and copying the inputs to a bucket."""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import fsspec
import pandas as pd
import pytest
import yaml
from fsspec.implementations.memory import MemoryFileSystem
from pydantic import ValidationError

from runtraj.execution import inputs, kubernetes, runner
from runtraj.execution.config import ExecutionConfig, KubernetesConfig
from runtraj.execution.inputs import hash_inputs, pull, push
from runtraj.project import Project
from runtraj.releases import PointRelease
from tests.fixtures.platform import not_on_windows

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
    assert _execution(fetch=True).fetch is True


def test_a_setting_beside_backend_names_the_kubernetes_block():
    with pytest.raises(ValidationError, match="execution: kubernetes:"):
        ExecutionConfig.model_validate({"backend": "kubernetes", "completions": 4})


def test_a_url_with_one_slash_is_refused():
    with pytest.raises(ValidationError, match="did you mean 's3://bucket/p'"):
        KubernetesConfig(inputs="s3:/bucket/p")


def test_a_volume_path_must_be_absolute():
    with pytest.raises(ValidationError):
        KubernetesConfig(volumes=[{"claim_name": "met", "mount_path": "met"}])


def _volume(claim_name="met", mount_path="/met"):
    return {"claim_name": claim_name, "mount_path": mount_path}


@pytest.mark.parametrize(
    ("volume", "message"),
    [
        (_volume(mount_path="/scratch"), "where each pod keeps its inputs"),
        (_volume(mount_path="/scratch/"), "where each pod keeps its inputs"),
        (_volume(mount_path="/"), "the pod's root"),
        (_volume(mount_path="/met:ro"), "has a ':'.*read_only"),
        (
            _volume(claim_name="/data/met"),
            "claim_name '/data/met' is not a Kubernetes name",
        ),
        (_volume(claim_name=""), "claim_name '' is not a Kubernetes name"),
        (_volume(claim_name="Hrrr_Arl"), "such as 'hrrr-arl'"),
    ],
)
def test_a_volume_that_would_fail_in_the_pod_is_refused(volume, message):
    with pytest.raises(ValidationError, match=message):
        KubernetesConfig(volumes=[volume])


def test_two_volumes_at_one_path_are_refused():
    with pytest.raises(ValidationError, match="'a' and 'b' are both mounted at /met"):
        KubernetesConfig(volumes=[_volume("a"), _volume("b", "/met/")])


def test_volumes_with_kubernetes_names_and_apart_paths_pass():
    config = KubernetesConfig(volumes=[_volume("hrrr-arl.v2"), _volume("out", "/out")])
    assert [v.mount_path for v in config.volumes] == ["/met", "/out"]


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("secrets", [""]),
        ("secrets", ["Bad_Name"]),
        ("service_account", "Bad_Name"),
        ("namespace", "Bad NS"),
        ("namespace", "my.namespace"),
    ],
)
def test_a_name_kubernetes_would_refuse_is_refused(setting, value):
    with pytest.raises(ValidationError, match="is not a Kubernetes name"):
        KubernetesConfig(**{setting: value})


def test_names_kubernetes_accepts_pass():
    config = KubernetesConfig(
        secrets=["bucket-keys"], service_account="runner", namespace="runs-2"
    )
    assert (config.secrets, config.service_account, config.namespace) == (
        ["bucket-keys"],
        "runner",
        "runs-2",
    )


@pytest.mark.parametrize("image", ["", "my image:latest", " registry/x:1"])
def test_an_image_that_is_empty_or_has_a_space_is_refused(image):
    with pytest.raises(ValidationError, match="is not an image name"):
        KubernetesConfig(image=image)


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


#: A project on a shared volume is a folder the pods open by the same path,
#: which only a Linux machine has; Windows refuses it (``_pods_open``).
_SHARED_VOLUME = not_on_windows("a project on a shared volume is submitted from Linux")


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


@_SHARED_VOLUME
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


def test_submit_from_windows_refuses_inputs_the_pods_cannot_open(
    tmp_path, kubectl, monkeypatch
):
    monkeypatch.setattr(kubernetes, "sys", SimpleNamespace(platform="win32"))
    with pytest.raises(ValueError, match="execution.kubernetes.inputs to a bucket"):
        runner.submit(_project(tmp_path, 1), execution=_execution())
    assert kubectl.created == []


def test_submit_from_windows_with_inputs_on_a_bucket_creates_the_job(
    tmp_path, kubectl, monkeypatch
):
    monkeypatch.setattr(kubernetes, "sys", SimpleNamespace(platform="win32"))
    project = _project(tmp_path, 1, output="memory://windows-bucket/out")
    runner.submit(project, execution=_execution(inputs="memory://windows-bucket/p"))
    assert len(kubectl.created) == 1


@_SHARED_VOLUME
def test_submit_says_why_kubectl_refused(tmp_path, kubectl):
    kubectl.answer = SimpleNamespace(returncode=1, stdout="", stderr="forbidden: quota")
    with pytest.raises(RuntimeError, match="forbidden: quota"):
        runner.submit(_project(tmp_path, 1), execution=_execution())


@_SHARED_VOLUME
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

    monkeypatch.setattr(MemoryFileSystem, "info", info)
    with pytest.raises(OSError, match="connection refused"):
        pull("memory://down/p", tmp_path / "pod")


# ---------------------------------------------------------------------------
# The project's Jobs, for runtraj status
# ---------------------------------------------------------------------------

#: A Job as ``kubectl get jobs -o json`` shows it, part way through.
RUNNING = {
    "metadata": {
        "name": "runtraj-hourly-x7k2p",
        "creationTimestamp": "2026-10-29T16:00:00Z",
    },
    "spec": {"completions": 20, "parallelism": 5},
    "status": {"active": 5, "completedIndexes": "0-9,12", "failedIndexes": "10,11"},
}
#: The same Job once it gave up on its failed tasks.
FAILED = {
    **RUNNING,
    "status": {
        "completedIndexes": "0-9,12-19",
        "failedIndexes": "10,11",
        "conditions": [
            {"type": "FailureTarget", "status": "True", "reason": "FailedIndexes"},
            {"type": "Failed", "status": "True", "reason": "FailedIndexes"},
        ],
    },
}
COMPLETE = {
    **RUNNING,
    "metadata": {
        "name": "runtraj-hourly-a1b2c",
        "creationTimestamp": "2026-10-28T09:00:00Z",
    },
    "status": {
        "completedIndexes": "0-19",
        "conditions": [{"type": "Complete", "status": "True"}],
    },
}


@pytest.mark.parametrize(("indexes", "n"), [("0-3,7", 5), ("4", 1), ("", 0), (None, 0)])
def test_count_indexes(indexes, n):
    assert kubernetes.count_indexes(indexes) == n


def test_a_running_job_s_summary_counts_its_tasks():
    assert kubernetes.job_summary(RUNNING) == {
        "name": "runtraj-hourly-x7k2p",
        "state": "running",
        "reason": None,
        "created": "2026-10-29T16:00:00Z",
        "tasks": 20,
        "done": 11,
        "failed": 2,
        "failed_tasks": "10,11",
        "running": 5,
    }


def test_a_job_s_state_is_failed_with_its_reason_complete_or_suspended():
    assert (
        kubernetes.job_summary(FAILED)["state"],
        kubernetes.job_summary(FAILED)["reason"],
    ) == ("failed", "FailedIndexes")
    assert kubernetes.job_summary(COMPLETE)["state"] == "complete"
    suspended = {**RUNNING, "spec": {"suspend": True}}
    assert kubernetes.job_summary(suspended)["state"] == "suspended"


def _claimed(job: dict, project: str | None) -> dict:
    """*job* with the project annotation *project*, or with none."""
    annotations = {} if project is None else {kubernetes.PROJECT_ANNOTATION: project}
    return {**job, "metadata": {**job["metadata"], "annotations": annotations}}


@pytest.fixture
def cluster(monkeypatch):
    """A fake ``kubectl get jobs``: ``jobs`` is what it lists, ``args`` what it was asked."""
    state = SimpleNamespace(jobs=[], args=[], answer=None)

    def fake_run(args, **kwargs):
        state.args.append(args)
        if state.answer is not None:
            return state.answer
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"items": state.jobs}), stderr=""
        )

    monkeypatch.setattr(kubernetes.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(kubernetes.subprocess, "run", fake_run)
    return state


def test_a_project_s_jobs_are_found_by_its_label_oldest_first(tmp_path, cluster):
    project = _project(tmp_path)
    where = str(project.directory.resolve())
    cluster.jobs = [_claimed(RUNNING, where), _claimed(COMPLETE, where)]

    jobs = kubernetes.project_jobs(project, _execution(namespace="runs"))

    assert [job["name"] for job in jobs] == [
        "runtraj-hourly-a1b2c",
        "runtraj-hourly-x7k2p",
    ]
    [args] = cluster.args
    assert args[1:5] == ["get", "jobs", "-l", f"{kubernetes.PROJECT_LABEL}=hourly"]
    assert args[-2:] == ["--namespace", "runs"]


def test_jobs_of_another_project_of_the_same_name_are_left_out(tmp_path, cluster):
    project = _project(tmp_path)
    cluster.jobs = [
        _claimed(RUNNING, "s3://lab-b/projects/hourly"),
        _claimed(COMPLETE, None),
        _claimed(FAILED, "S3://lab-a/projects/hourly/"),
    ]

    jobs = kubernetes.project_jobs(
        project, _execution(inputs="s3://lab-a/projects/hourly")
    )

    assert [(job["name"], job["state"]) for job in jobs] == [
        ("runtraj-hourly-x7k2p", "failed")
    ]


def test_a_project_reached_through_a_symlink_finds_its_jobs(tmp_path, cluster):
    project = _project(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(project.directory)
    cluster.jobs = [_claimed(RUNNING, str(project.directory.resolve()))]

    jobs = kubernetes.project_jobs(Project(link), _execution())

    assert len(jobs) == 1


def test_the_jobs_of_a_schedule_and_a_submit_are_listed_alike(tmp_path, cluster):
    project = _project(tmp_path)
    where = str(project.directory.resolve())
    cron = kubernetes.cron_job_manifest("hourly", where, _execution(), output="/out")
    hourly = {
        **cron["spec"]["jobTemplate"],
        "metadata": {
            **cron["spec"]["jobTemplate"]["metadata"],
            "name": "runtraj-hourly-1234abcd-29001",
        },
        "status": {"completedIndexes": "0"},
    }
    cluster.jobs = [hourly]

    [job] = kubernetes.project_jobs(project, _execution())

    assert (job["name"], job["done"], job["tasks"]) == (
        "runtraj-hourly-1234abcd-29001",
        1,
        1,
    )


def test_listing_jobs_says_why_kubectl_could_not(tmp_path, cluster):
    cluster.answer = SimpleNamespace(returncode=1, stdout="", stderr="Unauthorized")
    with pytest.raises(RuntimeError, match="could not list the Jobs: Unauthorized"):
        kubernetes.project_jobs(_project(tmp_path), _execution())


def test_listing_jobs_without_kubectl_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(kubernetes.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="kubectl is not installed"):
        kubernetes.project_jobs(_project(tmp_path), _execution())


# ---------------------------------------------------------------------------
# Getting the met once, before the pods run
# ---------------------------------------------------------------------------


@_SHARED_VOLUME
@pytest.mark.parametrize(
    ("fetch", "downloads", "asked"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
    ],
)
def test_the_pods_fetch_first_only_when_set_and_a_met_downloads(
    tmp_path, kubectl, monkeypatch, fetch, downloads, asked
):
    monkeypatch.setattr(kubernetes, "downloads", lambda project, variants: downloads)

    runner.submit(_project(tmp_path, 2), execution=_execution(fetch=fetch))

    [job] = kubectl.created
    args = job["spec"]["template"]["spec"]["containers"][0]["args"]
    assert ("--fetch" in args) is asked


@pytest.fixture
def fetches(monkeypatch):
    """Record each fetch the runner makes, and how many ran at once; ``downloads`` says whether a met downloads."""
    state = SimpleNamespace(calls=[], at_once=0, most=0, downloads=True)
    guard = threading.Lock()

    def fetch_project(project, unrun):
        with guard:
            state.at_once += 1
            state.most = max(state.most, state.at_once)
        time.sleep(0.05)
        state.calls.append(len(unrun))
        with guard:
            state.at_once -= 1
        return pd.DataFrame(columns=["met", "file", "message"])

    monkeypatch.setattr(runner, "fetch_project", fetch_project)
    monkeypatch.setattr(runner, "downloads", lambda project, variants: state.downloads)
    return state


@not_on_windows(
    "Windows cannot remove a lock file another task has open; the lock is for Kubernetes and Slurm tasks"
)
def test_tasks_that_share_the_inputs_fetch_one_at_a_time(
    tmp_path, fetches, monkeypatch
):
    monkeypatch.setattr(inputs, "LOCK_POLL_SECONDS", 0.01)
    project = _project(tmp_path, 3)
    where = str(project.directory)
    tasks = [
        threading.Thread(target=runner.fetch_shared, args=(Project(where), where))
        for _ in range(4)
    ]
    for task in tasks:
        task.start()
    for task in tasks:
        task.join(10)

    assert fetches.calls == [3]  # the others find the first's note, and list nothing
    assert fetches.most == 1
    assert (project.directory / runner.FETCH_DONE).read_text() == hash_inputs(
        project.directory
    )
    assert not (project.directory / ".fetch.lock").exists()


def test_no_lock_and_no_fetch_when_no_met_downloads(tmp_path, fetches, monkeypatch):
    fetches.downloads = False

    def no_lock(*args):
        raise AssertionError("locked")

    monkeypatch.setattr(runner, "lock", no_lock)
    project = _project(tmp_path, 1)

    runner.fetch_shared(project, str(project.directory))

    assert fetches.calls == []


def test_a_held_lock_is_kept_alive_past_the_stale_time(tmp_path, monkeypatch):
    """A pod downloading for longer than LOCK_STALE_SECONDS keeps its lock."""
    # 20 rewrites per stale time, so a busy CI runner that misses some
    # still keeps the lock; the hold is 2.5 stale times.
    monkeypatch.setattr(inputs, "LOCK_POLL_SECONDS", 0.01)
    monkeypatch.setattr(inputs, "LOCK_STALE_SECONDS", 1.0)
    monkeypatch.setattr(inputs, "LOCK_REFRESH_SECONDS", 0.05)
    order = []

    def second():
        with inputs.lock(str(tmp_path), "fetch"):
            order.append("second")

    with inputs.lock(str(tmp_path), "fetch"):
        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(2.5)
        order.append("first")
    thread.join(10)

    assert order == ["first", "second"]
    assert not (tmp_path / ".fetch.lock").exists()

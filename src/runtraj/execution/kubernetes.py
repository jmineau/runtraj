"""
Running a project as a Kubernetes indexed Job.

``runtraj submit`` with ``backend: kubernetes`` makes one indexed Job of
``completions`` tasks, each a pod that runs ``runtraj run <project> --task
I/N`` (:func:`job_manifest`), and creates it with ``kubectl``. Kubernetes
retries a task from its exit code (``podFailurePolicy``):

- 2, a wrong command line or invalid project files, or inputs changed after
  submit, fails the whole Job, since every task would fail the same way;
- 1, some simulations failed, fails that task only: running it again
  repeats the failure until the cause is fixed;
- 4, an unexpected error, fails that task only: a retry meets the same error;
- 3, stopped or a connection error, is retried, up to :data:`RETRIES` times
  per task;
- a pod evicted or preempted (a spot node taken back) is retried without
  counting against that.

Nothing here decides which simulations are finished: a task that runs
again skips the ones whose results exist.

``runtraj schedule`` makes a CronJob instead (:func:`cron_job_manifest`),
whose one pod an hour runs ``runtraj tick`` (:mod:`runtraj.execution.schedule`)
under the same rules.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import yaml

from runtraj._paths import is_url
from runtraj.execution.config import ExecutionConfig
from runtraj.execution.inputs import RELEASES, has_file, hash_inputs, push
from runtraj.execution.schedule import DELAY_HOURS, read_sites

if TYPE_CHECKING:
    from runtraj.project import Project

logger = logging.getLogger(__name__)

#: Retries per task for a pod that exits with 3 or crashes.
RETRIES = 3
#: Seconds a finished Job, and its pods' logs, stay on the cluster: a week.
KEEP_FINISHED = 7 * 24 * 3600
#: The label every Job and pod of a project carries, with the project's name.
PROJECT_LABEL = "runtraj/project"
#: The annotation that holds the project's URL or path.
PROJECT_ANNOTATION = "runtraj/project-url"
#: Where each pod keeps its copy of the inputs and its runs' workdirs: an ``emptyDir``.
SCRATCH = "/scratch"
#: The group that owns the pods' volumes, so a new volume, which a cloud
#: makes owned by root, is writable by an image that runs as another user.
FS_GROUP = 10001
#: The name of the container that runs ``runtraj``.
CONTAINER = "runtraj"
#: When a schedule's pod starts: 15 minutes past each hour, in UTC.
SCHEDULE = "15 * * * *"
#: Seconds a schedule's pod may run before its Job fails, unless
#: ``active_deadline_seconds`` is set: a pod that hangs (a stalled download,
#: a bucket call with no timeout) would otherwise hold back every later hour.
TICK_DEADLINE = 45 * 60


def label_value(name: str) -> str:
    """
    Return *name* as a Kubernetes label value and name prefix.

    Lowercase letters, digits and hyphens, at most 40 characters, starting
    and ending with a letter or digit.

    Examples
    --------
    >>> label_value("SLV_2015 winter")
    'slv-2015-winter'
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return slug or "project"


def pull_policy(image: str) -> str | None:
    """
    Return the ``imagePullPolicy`` for *image*: ``Always`` for a ``:main`` tag.

    A ``:main`` tag moves with every push, but Kubernetes pulls a tag other
    than ``latest`` only when the node lacks it, so a node would keep the
    build it pulled first and the pods of one Job could run different code.
    Other images keep Kubernetes' default (``None`` here), so an image loaded
    into a cluster by hand, as ``kind load`` does, still runs.

    Examples
    --------
    >>> pull_policy("ghcr.io/example/runtraj-hysplit:main")
    'Always'
    >>> pull_policy("ghcr.io/example/runtraj-hysplit:0.1.0") is None
    True
    """
    name = image.rsplit("/", 1)[-1]
    if "@" in name or ":" not in name:
        return None
    return "Always" if name.rpartition(":")[2] == "main" else None


def pod_args(
    project: str, n_tasks: int, cpus: int, output: str, input_hash: str
) -> list[str]:
    """
    Return the command line each pod runs: ``runtraj run <project> --task I/N``.

    Kubernetes puts each pod's index in ``JOB_COMPLETION_INDEX`` and
    replaces ``$(JOB_COMPLETION_INDEX)`` in the arguments with it.

    Examples
    --------
    >>> pod_args("s3://bucket/p", 20, 4, "s3://bucket/out", "ab12")[:5]
    ['runtraj', 'run', 's3://bucket/p', '--task', '$(JOB_COMPLETION_INDEX)/20']
    """
    return [
        "runtraj",
        "run",
        project,
        "--task",
        f"$(JOB_COMPLETION_INDEX)/{n_tasks}",
        "--cpus",
        str(cpus),
        "--output",
        output,
        "--inputs-hash",
        input_hash,
    ]


def _metadata(
    project_name: str, project: str, namespace: str | None, **naming: str
) -> dict[str, Any]:
    """Return an object's metadata: *naming* (``name`` or ``generateName``), labels, annotations, namespace."""
    metadata: dict[str, Any] = {
        **naming,
        "labels": _labels(project_name),
        "annotations": {PROJECT_ANNOTATION: project},
    }
    if namespace is not None:
        metadata["namespace"] = namespace
    return metadata


def _labels(name: str) -> dict[str, str]:
    """Return the labels every Job and pod of the project *name* carries."""
    return {"app.kubernetes.io/name": "runtraj", PROJECT_LABEL: label_value(name)}


def _job_spec(
    name: str,
    execution: ExecutionConfig,
    args: list[str],
    *,
    completions: int,
    parallelism: int,
    active_deadline_seconds: int | None,
) -> dict[str, Any]:
    """
    Return the ``spec`` of an indexed Job whose pods run *args*.

    Each pod gets the resources, volumes, secrets and service account of
    *execution*'s ``kubernetes`` block, an ``emptyDir`` at :data:`SCRATCH`
    as its ``TMPDIR`` and one thread per worker (``OMP_NUM_THREADS=1``).
    The exit code rules are in this module's docstring.
    """
    kube = execution.kubernetes
    if kube.image is None:
        raise ValueError("execution.kubernetes.image is required.")
    resources: dict[str, Any] = {"requests": {"cpu": str(kube.cpus)}}
    if kube.memory is not None:
        resources["requests"]["memory"] = kube.memory
        resources["limits"] = {"memory": kube.memory}
    container: dict[str, Any] = {
        "name": CONTAINER,
        "image": kube.image,
        "args": args,
        "env": [
            {"name": "TMPDIR", "value": SCRATCH},
            {"name": "OMP_NUM_THREADS", "value": "1"},
        ],
        "resources": resources,
        "volumeMounts": [{"name": "scratch", "mountPath": SCRATCH}]
        + [
            {"name": f"volume-{i}", "mountPath": v.mount_path, "readOnly": v.read_only}
            for i, v in enumerate(kube.volumes)
        ],
    }
    policy = pull_policy(kube.image)
    if policy is not None:
        container["imagePullPolicy"] = policy
    if kube.secrets:
        container["envFrom"] = [{"secretRef": {"name": s}} for s in kube.secrets]
    pod: dict[str, Any] = {
        "restartPolicy": "Never",
        "containers": [container],
        "volumes": [{"name": "scratch", "emptyDir": {}}]
        + [
            {
                "name": f"volume-{i}",
                "persistentVolumeClaim": {"claimName": v.claim_name},
            }
            for i, v in enumerate(kube.volumes)
        ],
        # The volume is given to FS_GROUP once (OnRootMismatch), not on
        # every pod start, which would walk a whole met archive.
        "securityContext": {
            "fsGroup": FS_GROUP,
            "fsGroupChangePolicy": "OnRootMismatch",
        },
    }
    if kube.service_account is not None:
        pod["serviceAccountName"] = kube.service_account

    def on_exit(action: str, code: int) -> dict[str, Any]:
        """Return the rule that takes *action* when ``runtraj`` exits with *code*."""
        rule = {"containerName": CONTAINER, "operator": "In", "values": [code]}
        return {"action": action, "onExitCodes": rule}

    spec: dict[str, Any] = {
        "completionMode": "Indexed",
        "completions": completions,
        "parallelism": parallelism,
        "backoffLimitPerIndex": RETRIES,
        "podFailurePolicy": {
            "rules": [
                on_exit("FailJob", 2),
                on_exit("FailIndex", 1),
                on_exit("FailIndex", 4),
                # `status` is required in a CronJob's template; the API
                # server fills it in only for a Job made on its own.
                {
                    "action": "Ignore",
                    "onPodConditions": [{"type": "DisruptionTarget", "status": "True"}],
                },
            ]
        },
        "ttlSecondsAfterFinished": KEEP_FINISHED,
        "template": {"metadata": {"labels": _labels(name)}, "spec": pod},
    }
    if active_deadline_seconds is not None:
        spec["activeDeadlineSeconds"] = active_deadline_seconds
    return spec


def job_manifest(
    name: str,
    project: str,
    execution: ExecutionConfig,
    n_tasks: int,
    *,
    output: str,
    input_hash: str,
) -> dict[str, Any]:
    """
    Return the indexed Job that runs a project in *n_tasks* pods, as a Kubernetes manifest.

    Its name is ``runtraj-<name>-`` and a suffix Kubernetes picks, so a
    project can be submitted again. Each pod runs :func:`pod_args`, with
    the resources, volumes, secrets and service account of *execution*'s
    ``kubernetes`` block, an ``emptyDir`` at :data:`SCRATCH` as its
    ``TMPDIR`` and one thread per worker (``OMP_NUM_THREADS=1``). The exit
    code rules are in this module's docstring.

    Parameters
    ----------
    name : str
        The project's name, for the Job's name and labels.
    project : str
        What the pods open: the URL of the inputs, or the project folder's
        path on a volume they mount.
    execution : ExecutionConfig
        Settings with a ``kubernetes`` block.
    n_tasks : int
        The Job's ``completions``.
    output : str
        The output directory the pods write to.
    input_hash : str
        :func:`runtraj.execution.inputs.hash_inputs` of the inputs submitted.
    """
    kube = execution.kubernetes
    spec = _job_spec(
        name,
        execution,
        pod_args(project, n_tasks, kube.cpus, output, input_hash),
        completions=n_tasks,
        parallelism=min(kube.parallelism or n_tasks, n_tasks),
        active_deadline_seconds=kube.active_deadline_seconds,
    )
    metadata = _metadata(
        name, project, kube.namespace, generateName=f"runtraj-{label_value(name)}-"
    )
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": metadata, "spec": spec}


def tick_args(project: str, cpus: int, output: str, delay: int) -> list[str]:
    """
    Return the command line a schedule's pod runs each hour: ``runtraj tick <project>``.

    Examples
    --------
    >>> tick_args("s3://bucket/p", 4, "s3://bucket/out", 8)[:4]
    ['runtraj', 'tick', 's3://bucket/p', '--delay']
    """
    return [
        "runtraj",
        "tick",
        project,
        "--delay",
        str(delay),
        "--cpus",
        str(cpus),
        "--output",
        output,
    ]


def project_key(project: str) -> str:
    """
    Return *project* as every spelling of it is written: no trailing slash, a lowercase scheme.

    A folder is given as its real path already (:func:`submit_job`).

    Examples
    --------
    >>> project_key("S3://bucket/projects/my_project/")
    's3://bucket/projects/my_project'
    """
    scheme, sep, rest = project.partition("://")
    if not sep:
        return project.rstrip("/") or "/"
    return f"{scheme.lower()}{sep}{rest.rstrip('/')}"


def cron_job_name(name: str, project: str) -> str:
    """
    Return the name of a project's CronJob: ``runtraj-<name>-<hash>``.

    The hash is of where the pods open the project (:func:`project_key`), so
    two projects whose folders share a name (``s3://lab-a/winter``,
    ``s3://lab-b/winter``) get two CronJobs, while scheduling one project
    again replaces its own. The name is cut to 35 characters, which keeps
    the whole within the 52 Kubernetes allows a CronJob.

    Examples
    --------
    >>> cron_job_name("winter", "s3://lab-a/projects/winter")
    'runtraj-winter-4a2ff0c8'
    """
    digest = hashlib.sha256(project_key(project).encode()).hexdigest()[:8]
    return f"runtraj-{label_value(name)[:35].rstrip('-')}-{digest}"


def cron_job_manifest(
    name: str,
    project: str,
    execution: ExecutionConfig,
    *,
    output: str,
    delay: int = DELAY_HOURS,
) -> dict[str, Any]:
    """
    Return the CronJob that runs ``runtraj tick`` for a project every hour.

    One pod at a time (a tick still running holds back the next), at 15
    minutes past each hour in UTC (:data:`SCHEDULE`), with the pods and
    exit-code rules of :func:`job_manifest`. Its Job fails after
    ``active_deadline_seconds``, or :data:`TICK_DEADLINE` when that is
    unset, so a pod that hangs does not stay active and, with
    ``concurrencyPolicy: Forbid``, block every later hour. The last day's
    Jobs are kept, with the project's label. It is named by
    :func:`cron_job_name`, so scheduling a project again replaces it.

    Parameters
    ----------
    name : str
        The project's name, for the CronJob's name and labels.
    project : str
        What the pods open: the URL of the inputs, or the project folder's
        path on a volume they mount.
    execution : ExecutionConfig
        Settings with a ``kubernetes`` block.
    output : str
        The output directory the pods write to.
    delay : int
        Hours before an hour is added
        (:data:`runtraj.execution.schedule.DELAY_HOURS`).
    """
    kube = execution.kubernetes
    spec = _job_spec(
        name,
        execution,
        tick_args(project, kube.cpus, output, delay),
        completions=1,
        parallelism=1,
        active_deadline_seconds=kube.active_deadline_seconds or TICK_DEADLINE,
    )
    metadata = _metadata(
        name, project, kube.namespace, name=cron_job_name(name, project)
    )
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": metadata,
        "spec": {
            "schedule": SCHEDULE,
            "timeZone": "Etc/UTC",
            "concurrencyPolicy": "Forbid",
            "startingDeadlineSeconds": 1800,
            "successfulJobsHistoryLimit": 24,
            "failedJobsHistoryLimit": 24,
            "jobTemplate": {
                # Kubernetes copies these to each Job it makes.
                "metadata": {
                    "labels": _labels(name),
                    "annotations": {PROJECT_ANNOTATION: project},
                },
                "spec": spec,
            },
        },
    }


def kubectl_create(manifest: dict[str, Any]) -> str:
    """
    Create *manifest* on the cluster ``kubectl`` points at, and return the object's name.

    Raises
    ------
    FileNotFoundError
        If ``kubectl`` is not on ``PATH``.
    RuntimeError
        If ``kubectl`` refuses it; the message is its error.
    """
    return _kubectl("create", manifest)


def kubectl_apply(manifest: dict[str, Any]) -> str:
    """
    Create *manifest*, or update the object of its name, and return the name.

    A schedule's CronJob has a fixed name (:func:`cron_job_name`), so
    scheduling a project again updates it.

    Raises
    ------
    FileNotFoundError
        If ``kubectl`` is not on ``PATH``.
    RuntimeError
        If ``kubectl`` refuses it; the message is its error.
    """
    return _kubectl("apply", manifest)


def _kubectl(verb: str, manifest: dict[str, Any]) -> str:
    """Run ``kubectl <verb> -f - -o name`` on *manifest* and return the object's name."""
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise FileNotFoundError("kubectl is not installed, or not on PATH.")
    result = subprocess.run(
        [kubectl, verb, "-f", "-", "-o", "name"],
        input=yaml.safe_dump(manifest, sort_keys=False),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"kubectl refused the {manifest['kind']}: "
            + (result.stderr.strip() or f"exit {result.returncode}")
        )
    return result.stdout.strip().split("/", 1)[-1]


def _pods_open(project: Project, execution: ExecutionConfig) -> tuple[str, str]:
    """
    Return what the pods open (the inputs' URL, or the folder's real path) and the output directory.

    Raises
    ------
    ValueError
        If the pods would lose their results: the inputs go to a bucket but
        the output directory is a path, so each pod would write to its own
        disk.
    """
    kube = execution.kubernetes
    output = str(project.output.directory)
    if kube.inputs is not None and not is_url(project.output.directory):
        raise ValueError(
            f"The output directory {output} is a path, so each pod would write "
            "its results to its own disk and lose them. Set output in "
            "config.yaml to a bucket, or give --output s3://..., with "
            "execution.kubernetes.inputs set."
        )
    return _target(project, execution), output


def _target(project: Project, execution: ExecutionConfig) -> str:
    """Return what the pods open: the inputs' URL, or the project folder's real path."""
    inputs = execution.kubernetes.inputs
    if inputs is not None:
        return inputs.rstrip("/")
    return str(Path(project.directory).resolve())


def count_indexes(indexes: str | None) -> int:
    """
    Return how many task indexes a Job's list of them holds, such as ``0-3,7``.

    Examples
    --------
    >>> count_indexes("0-3,7")
    5
    >>> count_indexes(None)
    0
    """
    total = 0
    for part in (indexes or "").split(","):
        if part:
            first, _, last = part.partition("-")
            total += int(last or first) - int(first) + 1
    return total


def job_summary(job: dict[str, Any]) -> dict[str, Any]:
    """
    Return what ``runtraj status`` shows of one Job, from ``kubectl get jobs -o json``.

    ``state`` is ``complete``, ``failed``, ``suspended`` or ``running``, and
    ``reason`` is Kubernetes' reason for a failed Job, such as
    ``FailedIndexes`` or ``DeadlineExceeded``. ``tasks`` is the Job's
    ``completions``; ``done``, ``failed`` and ``running`` count its tasks,
    and ``failed_tasks`` lists the ones that gave up as Kubernetes writes
    them (``1,4-6``).

    Examples
    --------
    >>> job = {
    ...     "metadata": {"name": "runtraj-towers-x7k2p"},
    ...     "spec": {"completions": 8},
    ...     "status": {"completedIndexes": "0-5", "failedIndexes": "7", "active": 1},
    ... }
    >>> summary = job_summary(job)
    >>> summary["state"], summary["done"], summary["failed_tasks"]
    ('running', 6, '7')
    """
    spec, status = job.get("spec", {}), job.get("status", {})
    conditions = {
        c["type"]: c for c in status.get("conditions", []) if c.get("status") == "True"
    }
    reason = None
    if "Complete" in conditions:
        state = "complete"
    elif "Failed" in conditions:
        state, reason = "failed", conditions["Failed"].get("reason")
    elif spec.get("suspend"):
        state = "suspended"
    else:
        state = "running"
    return {
        "name": job["metadata"]["name"],
        "state": state,
        "reason": reason,
        "created": job["metadata"].get("creationTimestamp"),
        "tasks": spec.get("completions"),
        "done": count_indexes(status.get("completedIndexes")),
        "failed": count_indexes(status.get("failedIndexes")),
        "failed_tasks": status.get("failedIndexes") or "",
        "running": status.get("active", 0),
    }


def project_jobs(project: Project, execution: ExecutionConfig) -> list[dict[str, Any]]:
    """
    Return the summaries of a project's Jobs on the cluster, oldest first (:func:`job_summary`).

    These are the indexed Jobs of ``runtraj submit`` and the hourly Jobs of
    ``runtraj schedule``, in the namespace of *execution*'s ``kubernetes``
    block. ``kubectl`` selects them by the project's label, which is only
    its name, so two projects can share it; a Job is kept only when its
    annotation names where this project's pods open it (the inputs' URL,
    or the folder's path).

    Raises
    ------
    FileNotFoundError
        If ``kubectl`` is not on ``PATH``.
    RuntimeError
        If ``kubectl`` cannot list the Jobs; the message is its error.
    """
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise FileNotFoundError("kubectl is not installed, or not on PATH.")
    args = [
        kubectl,
        "get",
        "jobs",
        "-l",
        f"{PROJECT_LABEL}={label_value(project.name)}",
        "-o",
        "json",
    ]
    namespace = execution.kubernetes.namespace
    if namespace is not None:
        args += ["--namespace", namespace]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "kubectl could not list the Jobs: "
            + (result.stderr.strip() or f"exit {result.returncode}")
        )
    target = project_key(_target(project, execution))
    jobs = [
        job_summary(item)
        for item in json.loads(result.stdout)["items"]
        if project_key(
            item["metadata"].get("annotations", {}).get(PROJECT_ANNOTATION, "")
        )
        == target
    ]
    return sorted(jobs, key=lambda job: job["created"] or "")


def _keep(project: Project, kind: str, manifest: dict[str, Any]) -> Path:
    """Write *manifest* to ``_kubernetes/<date_time>_<id>/<kind>.yaml`` in the project, and return that folder."""
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:6]}"
    folder = Path(project.directory) / "_kubernetes" / stamp
    folder.mkdir(parents=True)
    (folder / f"{kind}.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return folder


def submit_job(project: Project, pending: list[str], execution: ExecutionConfig) -> str:
    """
    Make the indexed Job that runs *pending* releases, and return its name.

    With ``inputs`` set, the project's inputs are copied there first
    (:func:`runtraj.execution.inputs.push`) and the pods open that URL;
    else they open the project folder at its path here, which must be on a
    volume they mount. The Job has ``completions`` tasks, or fewer when
    there are fewer releases. Its manifest is written to
    ``_kubernetes/<date_time>_<id>/job.yaml`` in the project before it is
    created, and the Job's name to ``job.name`` beside it.

    Raises
    ------
    ValueError
        If the pods would lose their results: the inputs go to a bucket but
        the output directory is a path, so each pod would write to its own
        disk.
    """
    kube = execution.kubernetes
    target, output = _pods_open(project, execution)
    input_hash = hash_inputs(project.directory)
    if kube.inputs is not None:
        push(project.directory, kube.inputs)
    n_tasks = max(1, min(kube.completions, len(pending)))
    manifest = job_manifest(
        project.name,
        target,
        execution,
        n_tasks,
        output=output,
        input_hash=input_hash,
    )
    folder = _keep(project, "job", manifest)
    name = kubectl_create(manifest)
    (folder / "job.name").write_text(name + "\n")
    logger.info(
        "Created Kubernetes Job %s: %d releases in %d tasks",
        name,
        len(pending),
        n_tasks,
    )
    return name


def schedule_job(
    project: Project, execution: ExecutionConfig, *, delay: int = DELAY_HOURS
) -> str:
    """
    Make the CronJob that runs a project every hour, for the sites in its ``sites.csv``, and return its name.

    Each hour its pod runs ``runtraj tick``
    (:mod:`runtraj.execution.schedule`). With ``inputs`` set, the project's
    inputs are copied there first, ``sites.csv`` with them; ``releases.csv``
    only while the bucket has none, since each hour's pod adds to the copy
    there and a push would write over those hours. A project needs no
    ``releases.csv`` to start: its first tick makes one. Without ``inputs`` the
    pods open the project folder at its path here, on a volume they mount.
    The manifest is written to ``_kubernetes/<date_time>_<id>/cronjob.yaml``
    in the project, and the CronJob's name to ``cronjob.name`` beside it.
    Scheduling the project again replaces its CronJob.

    Raises
    ------
    FileNotFoundError
        If the project has no ``sites.csv``.
    ValueError
        If ``sites.csv`` cannot be read, or the pods would lose their
        results (as :func:`submit_job`).
    """
    kube = execution.kubernetes
    sites = read_sites(project.directory)
    target, output = _pods_open(project, execution)
    if kube.inputs is not None:
        # A project starts with no releases.csv; its first tick makes one.
        here = (Path(project.directory) / RELEASES).is_file()
        push(
            project.directory,
            kube.inputs,
            releases=here and not has_file(kube.inputs, RELEASES),
        )
    manifest = cron_job_manifest(
        project.name, target, execution, output=output, delay=delay
    )
    folder = _keep(project, "cronjob", manifest)
    name = kubectl_apply(manifest)
    (folder / "cronjob.name").write_text(name + "\n")
    logger.info(
        "Scheduled Kubernetes CronJob %s: %d sites every hour, %d h behind",
        name,
        len(sites),
        delay,
    )
    return name


__all__ = [
    "FS_GROUP",
    "KEEP_FINISHED",
    "PROJECT_ANNOTATION",
    "PROJECT_LABEL",
    "RETRIES",
    "SCHEDULE",
    "SCRATCH",
    "TICK_DEADLINE",
    "cron_job_manifest",
    "count_indexes",
    "cron_job_name",
    "job_manifest",
    "job_summary",
    "kubectl_apply",
    "kubectl_create",
    "label_value",
    "pod_args",
    "project_jobs",
    "project_key",
    "pull_policy",
    "schedule_job",
    "submit_job",
    "tick_args",
]

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
"""

from __future__ import annotations

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
from runtraj.execution.inputs import hash_inputs, push

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
    if kube.image is None:
        raise ValueError("execution.kubernetes.image is required.")
    labels = {"app.kubernetes.io/name": "runtraj", PROJECT_LABEL: label_value(name)}
    metadata: dict[str, Any] = {
        "generateName": f"runtraj-{label_value(name)}-",
        "labels": labels,
        "annotations": {PROJECT_ANNOTATION: project},
    }
    if kube.namespace is not None:
        metadata["namespace"] = kube.namespace

    resources: dict[str, Any] = {"requests": {"cpu": str(kube.cpus)}}
    if kube.memory is not None:
        resources["requests"]["memory"] = kube.memory
        resources["limits"] = {"memory": kube.memory}
    container: dict[str, Any] = {
        "name": CONTAINER,
        "image": kube.image,
        "args": pod_args(project, n_tasks, kube.cpus, output, input_hash),
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
        "completions": n_tasks,
        "parallelism": min(kube.parallelism or n_tasks, n_tasks),
        "backoffLimitPerIndex": RETRIES,
        "podFailurePolicy": {
            "rules": [
                on_exit("FailJob", 2),
                on_exit("FailIndex", 1),
                on_exit("FailIndex", 4),
                {
                    "action": "Ignore",
                    "onPodConditions": [{"type": "DisruptionTarget", "status": "True"}],
                },
            ]
        },
        "ttlSecondsAfterFinished": KEEP_FINISHED,
        "template": {"metadata": {"labels": labels}, "spec": pod},
    }
    if kube.active_deadline_seconds is not None:
        spec["activeDeadlineSeconds"] = kube.active_deadline_seconds
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": metadata, "spec": spec}


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
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise FileNotFoundError("kubectl is not installed, or not on PATH.")
    result = subprocess.run(
        [kubectl, "create", "-f", "-", "-o", "name"],
        input=yaml.safe_dump(manifest, sort_keys=False),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "kubectl refused the Job: "
            + (result.stderr.strip() or f"exit {result.returncode}")
        )
    return result.stdout.strip().split("/", 1)[-1]


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
    output = str(project.output.directory)
    if kube.inputs is not None and not is_url(project.output.directory):
        raise ValueError(
            f"The output directory {output} is a path, so each pod would write "
            "its results to its own disk and lose them. Set output in "
            "config.yaml to a bucket, or give --output s3://..., with "
            "execution.kubernetes.inputs set."
        )
    input_hash = hash_inputs(project.directory)
    if kube.inputs is not None:
        push(project.directory, kube.inputs)
        target = kube.inputs.rstrip("/")
    else:
        target = str(project.directory.resolve())
    n_tasks = max(1, min(kube.completions, len(pending)))
    manifest = job_manifest(
        project.name,
        target,
        execution,
        n_tasks,
        output=output,
        input_hash=input_hash,
    )
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:6]}"
    folder = Path(project.directory) / "_kubernetes" / stamp
    folder.mkdir(parents=True)
    (folder / "job.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    name = kubectl_create(manifest)
    (folder / "job.name").write_text(name + "\n")
    logger.info(
        "Created Kubernetes Job %s: %d releases in %d tasks",
        name,
        len(pending),
        n_tasks,
    )
    return name


__all__ = [
    "FS_GROUP",
    "KEEP_FINISHED",
    "PROJECT_ANNOTATION",
    "PROJECT_LABEL",
    "RETRIES",
    "SCRATCH",
    "job_manifest",
    "kubectl_create",
    "label_value",
    "pod_args",
    "pull_policy",
    "submit_job",
]

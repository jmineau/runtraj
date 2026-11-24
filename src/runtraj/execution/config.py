"""Settings for where and how a project's releases are run."""

from __future__ import annotations

import difflib
import re
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class FetchConfig(BaseModel):
    """
    The Slurm resources of the job that gets the met files before a job array.

    Each setting left unset is the array's (``partition``, ``account``,
    ``qos``, ``time``, ``mem`` of :class:`SlurmConfig`). Set them to send
    the fetch to a data-transfer partition, where a cluster has one, and
    give ``time`` room for the downloads: a fetch job that runs out of time
    keeps the files it got, and the array then downloads the rest.

    Examples
    --------
    >>> config = SlurmConfig(
    ...     time="02:00:00",
    ...     fetch={"partition": "notchpeak-dtn", "account": "dtn", "time": "12:00:00"},
    ... )
    >>> config.fetch.partition
    'notchpeak-dtn'
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    partition: str | None = Field(
        None, description="Slurm partition of the fetch job. Unset is the array's."
    )
    account: str | None = Field(
        None, description="Slurm account of the fetch job. Unset is the array's."
    )
    qos: str | None = Field(
        None,
        description="Slurm quality of service of the fetch job. Unset is the array's.",
    )
    time: str | int | None = Field(
        None,
        description=(
            "Time limit of the fetch job, as for ``time``. Unset is the "
            "array's time limit per task."
        ),
    )
    mem: str | None = Field(
        None,
        description="Memory of the fetch job, such as ``4G``. Unset is the array's.",
    )

    @field_validator("time")
    @classmethod
    def _slurm_time(cls, value: str | int | None) -> str | int | None:
        """Raise on a time limit sbatch would not take."""
        if value is not None:
            slurm_minutes(value)  # raises on a form sbatch would not take
        return value


class BackendConfig(BaseModel):
    """
    A backend's block under ``execution``: the settings of one place a project runs.

    :class:`LocalConfig`, :class:`SlurmConfig` and :class:`KubernetesConfig`
    subclass it, and so does the block of a backend in another package
    (:class:`runtraj.execution.Backend`). Every block has ``cpus``, the
    releases a task runs at once, and ``fetch``, whether the met files are
    got once before the releases run. ``tasks`` is how many tasks share the
    releases, and :meth:`check` refuses a block its backend cannot run with.

    Examples
    --------
    >>> BackendConfig(cpus=2).tasks
    1
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpus: int = Field(1, ge=1, description="Releases a task runs at once.")
    fetch: bool | FetchConfig = Field(
        False,
        description="Get the met files a met with ``download`` lacks once, before the releases run.",
    )

    @property
    def tasks(self) -> int:
        """The number of tasks that share the releases (one, unless a subclass says more)."""
        return 1

    def check(self) -> None:
        """
        Raise ``ValueError`` when the block lacks a setting its backend needs.

        :class:`ExecutionConfig` calls it on the block ``backend`` names
        only, so a block that is not used may leave such a setting out.
        """


class LocalConfig(BackendConfig):
    """
    How a project's releases run on this machine: the ``local`` block of ``execution``.

    Examples
    --------
    >>> LocalConfig(cpus=4).cpus
    4
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpus: int = Field(
        1, ge=1, description="Releases run at once, each in a process of its own."
    )
    fetch: bool = Field(
        True,
        description=(
            "Get the met files a met with ``download`` lacks once, before the "
            "workers start, when ``cpus`` is above 1, so they do not download "
            "the same file at once. ``false`` leaves each run to download what "
            "it needs, as when something else fills the met directory."
        ),
    )


#: Array tasks per worker in a Slurm run with no ``n_tasks``.
TASKS_PER_WORKER = 10


class SlurmConfig(BackendConfig):
    """
    The Slurm job array a project's releases run as: the ``slurm`` block of ``execution``.

    The releases are split among ``n_tasks`` array tasks, at most
    ``n_workers`` of them running at once, and each task runs ``cpus`` of
    them at once. Unset, ``n_tasks`` is :data:`TASKS_PER_WORKER` per worker,
    so that a worker whose tasks run fast takes on more of them and one slow
    task does not hold up the run. Any key that is not a setting below is an
    ``sbatch`` option by its name, such as ``exclude: node1`` or
    ``constraint: skl``, written to the job script as given (``true``
    writes the bare flag) and able to replace a line the script would
    write. The fetch job takes ``clusters`` of these, and no other.

    Examples
    --------
    >>> config = SlurmConfig(n_workers=100, time="02:00:00", exclude="node1")
    >>> config.n_workers, config.tasks
    (100, 1000)
    >>> config.options
    {'exclude': 'node1'}
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    n_workers: int = Field(
        1,
        ge=1,
        description=(
            "Most array tasks running at once (``--array=0-999%100`` for 100). "
            "Each task asks for ``cpus`` CPUs."
        ),
    )
    n_tasks: int | None = Field(
        None,
        ge=1,
        description=(
            "Number of array tasks, which the releases are split evenly among. "
            "Unset, ten per worker. Never more than one per release, nor more "
            "than the cluster's ``MaxArraySize`` (``scontrol show config``)."
        ),
    )
    cpus: int = Field(
        1,
        ge=1,
        description="Releases each task runs at once, and the CPUs each task asks for.",
    )
    time: str | int | None = Field(
        None,
        description=(
            "Time limit per task, as ``HH:MM:SS``, ``D-HH:MM:SS``, or "
            "minutes. Required with ``backend: slurm``: without it a task gets "
            "the partition's default, which can be days."
        ),
    )
    mem: str | None = Field(None, description="Memory per task, such as ``8G``.")
    partition: str | None = Field(None, description="Slurm partition.")
    account: str | None = Field(None, description="Slurm account.")
    qos: str | None = Field(None, description="Slurm quality of service.")
    setup: list[str] = Field(
        default_factory=list,
        description=(
            "Shell commands run in each task before the worker, such as "
            "``module load`` lines. They run after the script exports "
            "``OMP_NUM_THREADS``, one thread per worker, so a line here can "
            "change it. The worker itself runs with the Python that submitted it."
        ),
    )
    fetch: FetchConfig | Literal[False] = Field(
        default_factory=FetchConfig,
        description=(
            "Get the met files a met with ``download`` lacks once, in a job of "
            "its own that the array waits for, with these resources "
            "(:class:`FetchConfig`). ``false`` leaves each run to download "
            "what it needs, as when something else fills the met directory."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _no_misspelled_setting(cls, data: Any) -> Any:
        """Refuse a key that reads as a misspelled setting, which would otherwise go to ``sbatch`` as an option."""
        if isinstance(data, dict):
            if "array_parallelism" in data:
                raise ValueError(
                    "array_parallelism is now n_workers, the most array tasks "
                    "running at once; n_tasks is the number of tasks."
                )
            for key in data:
                if key in cls.model_fields:
                    continue
                close = difflib.get_close_matches(
                    str(key), list(cls.model_fields), n=1, cutoff=0.8
                )
                if close:
                    raise ValueError(
                        f"{key!r} is not a setting of execution.slurm. Did you "
                        f"mean {close[0]!r}?"
                    )
        return data

    @field_validator("fetch", mode="before")
    @classmethod
    def _fetch_true(cls, value: Any) -> Any:
        """Read ``fetch: true`` (or an empty ``fetch:``) as a fetch job with the array's resources."""
        return {} if value is True or value is None else value

    @field_validator("setup", mode="before")
    @classmethod
    def _one_command(cls, value: Any) -> Any:
        """Accept one setup command as a string."""
        return [value] if isinstance(value, str) else value

    @field_validator("time")
    @classmethod
    def _slurm_time(cls, value: str | int | None) -> str | int | None:
        """Raise on a time limit sbatch would not take."""
        if value is not None:
            slurm_minutes(value)  # raises on a form sbatch would not take
        return value

    @property
    def options(self) -> dict[str, Any]:
        """The other ``sbatch`` options, by name: every key of the block that is not one of its settings."""
        return dict(self.model_extra or {})

    @property
    def tasks(self) -> int:
        """The number of array tasks, ``n_tasks``, or ten per worker when it is unset; fewer when there are fewer releases."""
        return (
            self.n_tasks
            if self.n_tasks is not None
            else TASKS_PER_WORKER * self.n_workers
        )

    def check(self) -> None:
        """Raise when the array has no time limit."""
        if self.time is None:
            raise ValueError(
                "execution.slurm.time is required with backend: slurm, such as "
                "time: '02:00:00'. Without it each task gets the partition's "
                "default time limit, which can be days."
            )


#: A Kubernetes memory quantity in its binary units: ``512Mi``, ``8Gi``, ``1.5Ti``.
_K8S_MEMORY = r"\d+(\.\d+)?(Ki|Mi|Gi|Ti|Pi|Ei)"


#: Where each pod keeps its copy of the inputs and its runs' workdirs: an ``emptyDir``.
SCRATCH = "/scratch"
_DNS_LABEL = r"[a-z0-9]([-a-z0-9]*[a-z0-9])?"
_DNS_SUBDOMAIN = rf"{_DNS_LABEL}(\.{_DNS_LABEL})*"


def check_kube_name(value: str, kind: str, *, label: bool = False) -> str:
    """
    Return *value* if it is a Kubernetes name, else raise ``ValueError`` naming *kind*.

    Kubernetes names its objects with lowercase letters, digits and ``-``
    (and ``.`` between parts, except in a namespace, which is one *label*),
    starting and ending with a letter or digit, so a name outside that
    would be refused by ``kubectl`` only when the Job is made.

    Examples
    --------
    >>> check_kube_name("hrrr-arl", "claim_name")
    'hrrr-arl'
    >>> check_kube_name("Bad_Name", "claim_name")
    Traceback (most recent call last):
    ...
    ValueError: claim_name 'Bad_Name' is not a Kubernetes name: use lowercase letters, digits and '-', starting and ending with a letter or digit, such as 'bad-name'.
    """
    pattern, longest = (_DNS_LABEL, 63) if label else (_DNS_SUBDOMAIN, 253)
    if len(value) <= longest and re.fullmatch(pattern, value):
        return value
    guess = re.sub(r"[^a-z0-9.]+", "-", value.lower()).strip("-.")
    hint = f", such as {guess!r}" if guess and guess != value else ""
    raise ValueError(
        f"{kind} {value!r} is not a Kubernetes name: use lowercase letters, "
        f"digits and '-', starting and ending with a letter or digit{hint}."
    )


class Volume(BaseModel):
    """
    A PersistentVolumeClaim mounted in every pod: one entry of ``volumes`` in the ``kubernetes`` block.

    Examples
    --------
    >>> Volume(claim_name="hrrr-arl", mount_path="/met", read_only=True).mount_path
    '/met'
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_name: str = Field(..., description="Name of the PersistentVolumeClaim.")
    mount_path: str = Field(
        ...,
        pattern=r"^/",
        description="Where the volume is mounted in each pod, an absolute path.",
    )
    read_only: bool = Field(
        False,
        description=("Mount it read-only, as for a met archive the pods only read."),
    )

    @field_validator("claim_name")
    @classmethod
    def _claim_name(cls, value: str) -> str:
        """Refuse a claim name Kubernetes would refuse, such as a path or ``''``."""
        return check_kube_name(value, "claim_name")

    @field_validator("mount_path")
    @classmethod
    def _mount_path(cls, value: str) -> str:
        """Refuse a mount path with a ``:``, or that is ``/`` or the pods' scratch."""
        if ":" in value:
            raise ValueError(
                f"mount_path {value!r} has a ':'; write the path alone and set "
                "read_only for a read-only mount."
            )
        if value.rstrip("/") == "":
            raise ValueError(
                f"mount_path {value!r} is the pod's root; mount the volume in a folder."
            )
        if value.rstrip("/") == SCRATCH:
            raise ValueError(
                f"mount_path {value!r} is where each pod keeps its inputs and "
                "workdirs; mount the volume at another path."
            )
        return value


class KubernetesConfig(BackendConfig):
    """
    The Kubernetes indexed Job a project's releases run as: the ``kubernetes`` block of ``execution``.

    The releases are split among ``completions`` tasks, each a pod that
    runs ``cpus`` of them at once. The settings use Kubernetes' own names
    and units. ``image`` is required: runtraj publishes no image, since an
    image has to carry the transport model (HYSPLIT's ``hycs_std``) and the
    stages a project names.

    Examples
    --------
    >>> config = KubernetesConfig(
    ...     image="registry.example.org/runtraj-hysplit:1",
    ...     completions=20,
    ...     memory="8Gi",
    ...     volumes=[
    ...         {"claim_name": "hrrr-arl", "mount_path": "/met", "read_only": True}
    ...     ],
    ... )
    >>> config.completions, config.volumes[0].claim_name
    (20, 'hrrr-arl')
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str | None = Field(
        None,
        description=(
            "Container image of the pods, with runtraj, the transport model and "
            "the project's stages. Required with ``backend: kubernetes``."
        ),
    )
    completions: int = Field(
        1,
        ge=1,
        description="Number of tasks (pods to complete), which the releases are split evenly among.",
    )
    parallelism: int | None = Field(
        None,
        ge=1,
        description="Most pods running at once. Unset runs every task at once.",
    )
    cpus: int = Field(
        1,
        ge=1,
        description="Releases each pod runs at once, and the CPUs each pod requests.",
    )
    memory: str | None = Field(
        None,
        description=(
            "Memory each pod requests, and its limit, in Kubernetes' binary "
            "units such as ``8Gi`` or ``512Mi``."
        ),
    )
    namespace: str | None = Field(
        None, description="Namespace of the Job. Unset is ``kubectl``'s current one."
    )
    inputs: str | None = Field(
        None,
        description=(
            "Bucket URL the project's inputs (``config.yaml``, ``releases.csv``, "
            "``tables/``, ``sites.csv``) are copied to at submit, such as "
            "``s3://bucket/projects/my_project``; each pod copies them back. "
            "Unset, the pods open the project folder at the path it has here, "
            "so it must be on a volume they mount at that path."
        ),
    )
    volumes: list[Volume] = Field(
        default_factory=list,
        description=(
            "PersistentVolumeClaims mounted in every pod (:class:`Volume`), "
            "such as the met archive."
        ),
    )
    secrets: list[str] = Field(
        default_factory=list,
        description=(
            "Secrets whose keys become each pod's environment variables, such "
            "as a bucket's credentials."
        ),
    )
    service_account: str | None = Field(
        None,
        description=(
            "Service account of the pods, on a cluster that grants bucket "
            "access through one."
        ),
    )
    active_deadline_seconds: int | None = Field(
        None,
        ge=1,
        description=(
            "Seconds the whole Job may run before Kubernetes stops it. Unset "
            "lets it run as long as its simulations take."
        ),
    )
    fetch: bool = Field(
        False,
        description=(
            "Get the met files once, before the pods run, for a met with "
            "``download`` whose directory is on a volume every pod mounts: "
            "each pod first gets the files of every unfinished simulation, "
            "one pod at a time (a lock where the inputs are), so the first "
            "downloads them and the others find them. Leave it ``false`` "
            "when each pod has its own met directory."
        ),
    )

    @field_validator("image")
    @classmethod
    def _image(cls, value: str | None) -> str | None:
        """Refuse an image that is empty or has a space."""
        if value is not None and (not value or re.search(r"\s", value)):
            raise ValueError(
                f"image {value!r} is not an image name: it is empty or has a "
                "space. Write it as in a Dockerfile's FROM, such as "
                "registry.example.org/runtraj-hysplit:1."
            )
        return value

    @field_validator("namespace")
    @classmethod
    def _namespace(cls, value: str | None) -> str | None:
        """Refuse a namespace that is not a Kubernetes label."""
        return (
            value if value is None else check_kube_name(value, "namespace", label=True)
        )

    @field_validator("service_account")
    @classmethod
    def _service_account(cls, value: str | None) -> str | None:
        """Refuse a service account that is not a Kubernetes name."""
        return value if value is None else check_kube_name(value, "service_account")

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, value: list[str]) -> list[str]:
        """Refuse a secret that is not a Kubernetes name."""
        for name in value:
            check_kube_name(name, "secrets entry")
        return value

    @model_validator(mode="after")
    def _volumes_apart(self) -> Self:
        """Refuse two volumes at one ``mount_path``."""
        seen: dict[str, str] = {}
        for volume in self.volumes:
            path = volume.mount_path.rstrip("/")
            if path in seen:
                raise ValueError(
                    f"volumes {seen[path]!r} and {volume.claim_name!r} are both "
                    f"mounted at {path}; each needs its own mount_path."
                )
            seen[path] = volume.claim_name
        return self

    @field_validator("memory")
    @classmethod
    def _kubernetes_memory(cls, value: str | None) -> str | None:
        """Refuse a size that is not in Kubernetes' binary units, such as Slurm's ``8G``."""
        if value is None or re.fullmatch(_K8S_MEMORY, value):
            return value
        slurm = re.fullmatch(r"(\d+(\.\d+)?)\s*([KMGT])B?", value, flags=re.I)
        hint = (
            f" Did you mean {slurm.group(1)}{slurm.group(3).upper()}i?" if slurm else ""
        )
        raise ValueError(
            f"memory {value!r} is not a Kubernetes size such as 8Gi or 512Mi "
            f"(Kubernetes reads 8G as 8 * 1000**3 bytes and 8m as 0.008).{hint}"
        )

    @field_validator("inputs")
    @classmethod
    def _a_whole_url(cls, value: str | None) -> str | None:
        """Refuse a URL typed with one slash, such as ``s3:/bucket``."""
        if value is not None:
            check_url(value)
        return value

    @property
    def tasks(self) -> int:
        """The number of tasks, ``completions``."""
        return self.completions

    def check(self) -> None:
        """Raise when the pods have no image."""
        if self.image is None:
            raise ValueError(
                "execution.kubernetes.image is required with backend: kubernetes: "
                "an image with runtraj, the transport model and the project's "
                "stages (runtraj publishes none)."
            )


def check_url(location: str) -> None:
    """
    Refuse a URL typed without its two slashes, such as ``s3:/bucket``.

    fsspec reads ``s3:/bucket/projects/my_project`` as a path on this
    machine, so copying there would make a folder ``s3:`` here and look
    like it worked. A path and a whole URL pass.

    Raises
    ------
    ValueError
        If *location* starts like a URL (``s3:``, ``gs:``) with fewer than two
        slashes after the colon.

    Examples
    --------
    >>> check_url("s3://bucket/projects/my_project")
    >>> check_url("s3:/bucket/projects/my_project")
    Traceback (most recent call last):
    ...
    ValueError: 's3:/bucket/projects/my_project' is not a URL; did you mean 's3://bucket/projects/my_project'?
    """
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9+.-]+):(?!//)/?(.*)", location, re.S)
    if match is None or match.group(1).lower() == "file":
        return
    scheme, rest = match.groups()
    raise ValueError(
        f"{location!r} is not a URL; did you mean {f'{scheme}://{rest}'!r}?"
    )


def _did_you_mean(key: str, blocks: dict[str, type[BackendConfig]]) -> str:
    """Return `` Did you mean ...?`` naming the setting of ``execution``, or of a block, closest to *key*, or ``""``."""
    names = {name: "" for name in ExecutionConfig.model_fields}
    for block, config_class in blocks.items():
        for name in config_class.model_fields:
            names.setdefault(name, f", under execution: {block}:")
    close = difflib.get_close_matches(str(key), list(names), n=1, cutoff=0.8)
    return f" Did you mean {close[0]!r}{names[close[0]]}?" if close else ""


#: The blocks of the backends built in, by the name ``backend`` gives them.
BUILT_IN_BLOCKS: dict[str, type[BackendConfig]] = {
    "local": LocalConfig,
    "slurm": SlurmConfig,
    "kubernetes": KubernetesConfig,
}


def block_name(backend: str) -> str:
    """
    Return the name of the block under ``execution`` that holds *backend*'s settings.

    A built-in backend's block has its name. A backend named by its import
    path has the block of its class's ``name``, so the class is imported.

    Examples
    --------
    >>> block_name("slurm")
    'slurm'

    Raises
    ------
    ValueError
        If *backend* is not a built-in backend and not an import path.
    MissingClass
        If *backend* is an import path that cannot be imported here.
    """
    if backend in BUILT_IN_BLOCKS:
        return backend
    from runtraj.execution.backends import get_backend

    return get_backend(backend).name


class ExecutionConfig(BaseModel):
    """
    Where a project's releases run, and with what resources.

    ``backend`` names the place, and every setting of that place is in the
    block of its name. With ``backend: local`` the releases run on this
    machine as one task (:class:`LocalConfig`). With ``backend: slurm``
    they are split among the tasks of one Slurm job array
    (:class:`SlurmConfig`), and with ``backend: kubernetes`` among the pods
    of one indexed Job (:class:`KubernetesConfig`). A backend in another
    package is named by the import path of its class
    (:class:`runtraj.execution.Backend`), and its block is under the
    class's ``name``. Only the block ``backend`` names is read, so a config
    can hold several and be run either way. ``timeout`` and
    ``keep_workdir`` apply wherever a run happens. None of these settings
    change a result.

    Examples
    --------
    >>> config = ExecutionConfig(
    ...     backend="slurm", slurm={"n_workers": 100, "cpus": 4, "time": "02:00:00"}
    ... )
    >>> config.slurm.n_workers
    100
    >>> config.cpus
    4
    """

    # A backend in another package keeps its block as an extra key, under
    # its name; any other key is refused (_known_keys).
    model_config = ConfigDict(extra="allow", frozen=True)

    backend: str = Field(
        "local",
        description=(
            "Where to run: on this machine (``local``), as a Slurm job array "
            "(``slurm``), as a Kubernetes indexed Job (``kubernetes``), or with "
            "a backend in another package, named by the import path of its "
            "class, such as ``mypkg.backends.MyBackend``."
        ),
    )
    local: LocalConfig = Field(
        default_factory=LocalConfig,
        description="How a local run goes (:class:`LocalConfig`). Read with ``backend: local``.",
    )
    slurm: SlurmConfig = Field(
        default_factory=SlurmConfig,
        description=(
            "The job array of a Slurm run (:class:`SlurmConfig`), and any "
            "other ``sbatch`` option by name. Read with ``backend: slurm``."
        ),
    )
    kubernetes: KubernetesConfig = Field(
        default_factory=KubernetesConfig,
        description=(
            "The indexed Job of a Kubernetes run (:class:`KubernetesConfig`). "
            "Read with ``backend: kubernetes``."
        ),
    )
    timeout: int | None = Field(
        None,
        ge=1,
        description=(
            "Time limit for one transport model run, in seconds. A run that "
            "exceeds it is stopped and recorded as a failed simulation, and the "
            "worker moves on. Unset waits indefinitely, so a hung run can hold "
            "a worker until its job ends."
        ),
    )
    keep_workdir: bool = Field(
        False,
        description=(
            "Keep every run's workdir, as the transport model left it, under "
            "``scratch/`` in the output directory. A failed run's is always "
            "kept, without the model's particle file unless this is set."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _known_keys(cls, data: Any) -> Any:
        """
        Read a backend in another package's block, and refuse any other key that is not a setting.

        A backend's setting written beside ``backend`` is refused naming the
        block it belongs in.
        """
        if not isinstance(data, dict):
            return data
        blocks = dict(BUILT_IN_BLOCKS)
        backend = data.get("backend", "local")
        if isinstance(backend, str) and backend not in BUILT_IN_BLOCKS:
            from runtraj.execution.backends import get_backend

            found = get_backend(backend)
            if found.name in cls.model_fields:
                raise ValueError(
                    f"{backend} names its block {found.name!r}, which is a "
                    "setting of execution; its name must be another."
                )
            blocks[found.name] = found.config_class
            data = {
                **data,
                found.name: found.config_class.model_validate(
                    data.get(found.name) or {}
                ),
            }
        for key in data:
            if key in cls.model_fields or key in blocks:
                continue
            owners = [
                name for name, block in blocks.items() if key in block.model_fields
            ]
            if owners:
                names = [f"execution: {name}:" for name in owners]
                where = " or ".join(
                    [", ".join(names[:-1]), names[-1]] if len(names) > 2 else names
                )
                raise ValueError(
                    f"{key!r} is a setting of a backend. Write it under {where}"
                    + (", for the backend it is for." if len(owners) > 1 else ".")
                )
            raise ValueError(
                f"{key!r} is not a setting of execution." + _did_you_mean(key, blocks)
            )
        return data

    @model_validator(mode="after")
    def _block_can_run(self) -> Self:
        """Raise when the block ``backend`` names lacks a setting its backend needs."""
        self.block.check()
        return self

    @property
    def block(self) -> BackendConfig:
        """The block of the backend ``backend`` names, the one a run reads."""
        name = block_name(self.backend)
        if name in BUILT_IN_BLOCKS:
            return getattr(self, name)
        return (self.model_extra or {})[name]

    @property
    def cpus(self) -> int:
        """Releases a task runs at once: ``cpus`` of the block ``backend`` names."""
        return self.block.cpus

    @property
    def fetch(self) -> bool | FetchConfig:
        """
        Whether the met files are got first: ``fetch`` of the block ``backend`` names.

        On Kubernetes each pod gets them before its share runs, one pod at a
        time (:ref:`fetch-once`).
        """
        return self.block.fetch


def slurm_minutes(time: str | int) -> int:
    """
    Return a Slurm time limit in whole minutes, rounded up.

    Accepts what ``sbatch --time`` does: minutes, ``MM:SS``, ``HH:MM:SS``,
    ``D-HH``, ``D-HH:MM``, and ``D-HH:MM:SS``.

    Raises
    ------
    ValueError
        If *time* has none of these forms or is not positive.
    """
    text = str(time).strip()
    days, _, clock = text.rpartition("-")
    parts = clock.split(":")
    try:
        if "-" in text and not days:
            raise ValueError
        numbers = [int(p) for p in parts]
        d = int(days) if days else 0
        if any(n < 0 for n in numbers) or d < 0:
            raise ValueError
        if days:  # D-HH[:MM[:SS]]
            hours, minutes, seconds = (numbers + [0, 0])[:3]
        elif len(numbers) == 1:  # minutes
            hours, minutes, seconds = 0, numbers[0], 0
        elif len(numbers) == 2:  # MM:SS
            hours, (minutes, seconds) = 0, numbers
        else:  # HH:MM:SS
            hours, minutes, seconds = numbers
        if len(numbers) > 3:
            raise ValueError
    except ValueError:
        raise ValueError(
            f"Not a Slurm time limit: {time!r}. Use minutes, 'HH:MM:SS', or "
            "'D-HH:MM:SS'."
        ) from None
    total = ((d * 24 + hours) * 60 + minutes) * 60 + seconds
    if total <= 0:
        raise ValueError(f"The time limit must be positive, not {time!r}.")
    return -(-total // 60)


__all__ = [
    "BUILT_IN_BLOCKS",
    "BackendConfig",
    "ExecutionConfig",
    "FetchConfig",
    "KubernetesConfig",
    "LocalConfig",
    "SlurmConfig",
    "TASKS_PER_WORKER",
    "Volume",
    "block_name",
    "check_url",
    "slurm_minutes",
]

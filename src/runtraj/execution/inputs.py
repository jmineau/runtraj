"""
Copying a project's inputs to and from a bucket, for pods that do not share a disk.

A project's inputs are its ``config.yaml``, its ``releases.csv`` and its
``tables/`` folder. :func:`push` copies them to a bucket (or any folder
fsspec can reach) when a Kubernetes Job is submitted, and each pod copies
them back to its own disk with :func:`pull`. The output directory is never
copied: the workers write results straight to it, wherever it is.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import fsspec

logger = logging.getLogger(__name__)

#: The files of a project folder a run reads.
INPUT_FILES = ("config.yaml", "releases.csv")
#: The folder of extra input tables (``Project.add_table``), copied when present.
TABLES = "tables"


def hash_inputs(directory: str | Path) -> str:
    """
    Return a content hash of a project folder's ``config.yaml`` and ``releases.csv``.

    ``runtraj submit`` hashes the inputs when it makes a Kubernetes Job and
    gives the hash to each pod, which hashes what it copied and refuses to
    run when the two differ: a ``releases.csv`` changed after submit would
    split the releases differently among the Job's tasks. The content is
    hashed, not a time or an etag, so copying the same files again is no
    change. ``tables/`` is not hashed: it does not decide the split.

    Raises
    ------
    FileNotFoundError
        If *directory* has no ``config.yaml`` or ``releases.csv``.
    """
    directory = Path(directory)
    digest = hashlib.sha256()
    for name in INPUT_FILES:
        path = directory / name
        if not path.is_file():
            raise FileNotFoundError(f"{directory} is not a project: it has no {name}.")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def push(directory: str | Path, url: str) -> list[str]:
    """
    Copy a project's inputs to *url*.

    Copies ``config.yaml``, ``releases.csv`` and, when the project has one,
    ``tables/``. Files already at *url* are replaced; files there that the
    project no longer has are left.

    Parameters
    ----------
    directory : str or Path
        The project folder on this machine.
    url : str
        Where to copy them, such as ``s3://bucket/projects/my_project``.

    Returns
    -------
    list of str
        The files copied, relative to the project folder.

    Raises
    ------
    FileNotFoundError
        If *directory* has no ``config.yaml`` or ``releases.csv``.
    """
    directory = Path(directory)
    for name in INPUT_FILES:
        if not (directory / name).is_file():
            raise FileNotFoundError(f"{directory} is not a project: it has no {name}.")
    fs, root = fsspec.core.url_to_fs(url)
    root = root.rstrip("/")
    fs.makedirs(root, exist_ok=True)
    names = list(INPUT_FILES)
    names += [
        path.relative_to(directory).as_posix()
        for path in sorted((directory / TABLES).rglob("*"))
        if path.is_file()
    ]
    for name in names:
        parent = name.rpartition("/")[0]
        if parent:
            fs.makedirs(f"{root}/{parent}", exist_ok=True)
        fs.put_file(str(directory / name), f"{root}/{name}")
    logger.info("Copied %d input files to %s", len(names), url)
    return names


def _exists(fs: fsspec.AbstractFileSystem, path: str, kind: str = "file") -> bool:
    """
    Whether *path* is a *kind* (``"file"`` or ``"directory"``) on *fs*.

    fsspec's ``isfile`` and ``isdir`` catch every error and answer
    ``False``, so a refused connection or expired credentials would read as
    a missing file. This lets every error but a missing *path* through.
    """
    try:
        info = fs.info(path)
    except FileNotFoundError:
        return False
    return info["type"] == kind


def pull(url: str, directory: str | Path) -> Path:
    """
    Copy a project's inputs from *url* into *directory*, the reverse of :func:`push`.

    Parameters
    ----------
    url : str
        Where the inputs are, such as ``s3://bucket/projects/my_project``.
    directory : str or Path
        The folder to copy them into, made if needed.

    Returns
    -------
    Path
        *directory*, now a project folder.

    Raises
    ------
    FileNotFoundError
        If *url* has no ``config.yaml`` or ``releases.csv``.
    OSError
        If *url* cannot be read, such as a refused connection, a timeout or
        expired credentials: unlike a missing project, a retry may get past
        it.
    """
    directory = Path(directory)
    fs, root = fsspec.core.url_to_fs(url)
    root = root.rstrip("/")
    try:
        for name in INPUT_FILES:
            if not _exists(fs, f"{root}/{name}"):
                raise FileNotFoundError(f"No project at {url}: it has no {name}.")
        directory.mkdir(parents=True, exist_ok=True)
        for name in INPUT_FILES:
            fs.get_file(f"{root}/{name}", str(directory / name))
        if _exists(fs, f"{root}/{TABLES}", kind="directory"):
            for remote in fs.find(f"{root}/{TABLES}"):
                local = directory / remote.removeprefix(f"{root}/")
                local.parent.mkdir(parents=True, exist_ok=True)
                fs.get_file(remote, str(local))
    except (FileNotFoundError, OSError):
        raise
    except Exception as error:
        # A cloud library's own error for a connection or credentials problem.
        raise OSError(f"cannot read {url}: {error}") from error
    logger.info("Copied the inputs at %s to %s", url, directory)
    return directory


__all__ = ["INPUT_FILES", "TABLES", "hash_inputs", "pull", "push"]

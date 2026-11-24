"""
Copying a project's inputs to and from a bucket, for pods that do not share a disk.

A project's inputs are its ``config.yaml``, its ``releases.csv``, its
``tables/`` folder, and the ``sites.csv`` of a project run every hour
(:mod:`runtraj.execution.schedule`). :func:`push` copies them to a bucket
(or any folder fsspec can reach) when a Kubernetes Job is submitted, and
each pod copies them back to its own disk with :func:`pull`. The output
directory is never copied: the workers write results straight to it,
wherever it is. A program that changes one of these files where it is, as
each hour's pod adds to ``releases.csv``, holds :func:`lock` while it does.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import fsspec
import fsspec.implementations.local

logger = logging.getLogger(__name__)

#: The file of a project's releases.
RELEASES = "releases.csv"
#: The files of a project folder a run reads.
INPUT_FILES = ("config.yaml", RELEASES)
#: The folder of extra input tables (``Project.add_table``), copied when present.
TABLES = "tables"
#: The places a schedule adds a release at each hour, copied when present.
SITES = "sites.csv"
#: Seconds a lock may stay the same before a program waiting for it takes it over.
LOCK_STALE_SECONDS = 300
#: Seconds between a waiting program's looks at a lock.
LOCK_POLL_SECONDS = 2
#: Seconds between a holder's rewrites of its lock, which keep it from
#: looking stale while the holder works for longer than :data:`LOCK_STALE_SECONDS`.
LOCK_REFRESH_SECONDS = 60
#: Times a holder tries to remove its lock when the system says the file is in use,
#: and the seconds between tries. Windows refuses to remove a file that a waiting
#: program has open for a look; the waiter closes it within moments.
LOCK_REMOVE_TRIES = 10
LOCK_REMOVE_WAIT_SECONDS = 0.05


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


def push(directory: str | Path, url: str, *, releases: bool = True) -> list[str]:
    """
    Copy a project's inputs to *url*.

    Copies ``config.yaml``, ``releases.csv`` and, when the project has them,
    ``sites.csv`` and ``tables/``. Files already at *url* are replaced.
    Files there that the project no longer has (a ``sites.csv`` or a table
    that was deleted here) are left, since a push never deletes, and are
    logged as a warning that names them: a pod and each hour's tick still
    read them, so delete them at *url* too.

    Parameters
    ----------
    directory : str or Path
        The project folder on this machine.
    url : str
        Where to copy them, such as ``s3://bucket/projects/my_project``.
    releases : bool
        Copy ``releases.csv``. A schedule leaves it out once the bucket has
        one, since each hour's pod adds to the copy there.

    Returns
    -------
    list of str
        The files copied, relative to the project folder.

    Raises
    ------
    FileNotFoundError
        If *directory* has no ``config.yaml``, or no ``releases.csv`` when
        *releases* is true.
    """
    directory = Path(directory)
    required = INPUT_FILES if releases else INPUT_FILES[:1]
    for name in required:
        if not (directory / name).is_file():
            raise FileNotFoundError(f"{directory} is not a project: it has no {name}.")
    fs, root = fsspec.core.url_to_fs(url)
    root = root.rstrip("/")
    fs.makedirs(root, exist_ok=True)
    names = [
        *required,
        *([SITES] if (directory / SITES).is_file() else []),
    ]
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
    stale = _stale(fs, root, names)
    if stale:
        logger.warning(
            "%s has %s, which this project does not: they are not removed, and "
            "the pods still read them. Delete them there to match the project.",
            url,
            ", ".join(stale),
        )
    return names


def _stale(fs: fsspec.AbstractFileSystem, root: str, names: list[str]) -> list[str]:
    """Return the ``sites.csv`` and tables under *root* that *names*, what was just copied, leaves out."""
    try:
        found = [str(p) for p in fs.find(f"{root}/{TABLES}")] + [f"{root}/{SITES}"]
    except FileNotFoundError:
        found = [f"{root}/{SITES}"]
    stale = []
    for path in sorted(found):
        name = path.removeprefix(f"{root}/")
        if name not in names and _exists(fs, path):
            stale.append(name)
    return stale


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

    A ``releases.csv`` is not required: a project run every hour has none
    until its first hour is added. ``sites.csv`` is copied when present.

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
        If *url* has no ``config.yaml``.
    OSError
        If *url* cannot be read, such as a refused connection, a timeout or
        expired credentials: unlike a missing project, a retry may get past
        it.
    """
    directory = Path(directory)
    fs, root = fsspec.core.url_to_fs(url)
    root = root.rstrip("/")
    try:
        if not _exists(fs, f"{root}/{INPUT_FILES[0]}"):
            raise FileNotFoundError(f"No project at {url}: it has no {INPUT_FILES[0]}.")
        directory.mkdir(parents=True, exist_ok=True)
        for name in (*INPUT_FILES, SITES):
            if name == INPUT_FILES[0] or _exists(fs, f"{root}/{name}"):
                fs.get_file(f"{root}/{name}", str(directory / name))
            else:
                # A copy from an earlier pull must not stand in for a file that is gone.
                (directory / name).unlink(missing_ok=True)
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


def has_file(url: str, name: str) -> bool:
    """
    Whether the project at *url* has the file *name*, such as ``releases.csv``.

    Raises
    ------
    OSError
        If *url* cannot be read; a missing file is ``False``, not an error.
    """
    fs, root = fsspec.core.url_to_fs(url)
    return _exists(fs, f"{root.rstrip('/')}/{name}")


def _atomic_put_file(fs: fsspec.AbstractFileSystem, lpath: str, rpath: str) -> None:
    """
    Copy *lpath* to *rpath* on *fs*, atomically when *fs* is local.

    ``fs.put_file`` truncates *rpath* and then writes it, so on a local
    filesystem or a mounted volume a reader that opens *rpath* meanwhile
    sees a half-written file. Here it is copied to a temporary name in the
    same folder and ``os.replace``'d onto *rpath*, which is atomic on one
    filesystem. S3 and Google Cloud Storage replace the whole object in one
    request already, so there it is written directly.
    """
    if not isinstance(fs, fsspec.implementations.local.LocalFileSystem):
        fs.put_file(lpath, rpath)
        return
    tmp = f"{rpath}.tmp-{uuid.uuid4().hex}"
    try:
        fs.put_file(lpath, tmp)
        os.replace(tmp, rpath)
    except Exception:
        with contextlib.suppress(Exception):
            fs.rm_file(tmp)
        raise


def put(directory: str | Path, url: str, name: str) -> None:
    """
    Copy one file of a project folder to *url*, such as a ``releases.csv`` that grew.

    Parameters
    ----------
    directory : str or Path
        The project folder on this machine.
    url : str
        Where the project's inputs are, as :func:`push` copied them.
    name : str
        The file, relative to the project folder.
    """
    fs, root = fsspec.core.url_to_fs(url)
    _atomic_put_file(fs, str(Path(directory) / name), f"{root.rstrip('/')}/{name}")


def get(url: str, directory: str | Path, name: str) -> bool:
    """
    Copy one file from *url* into a project folder, the reverse of :func:`put`.

    Returns whether *url* has the file. When it has none, the folder's own
    copy is removed, so an old copy cannot stand in for it.

    Raises
    ------
    OSError
        If *url* cannot be read.
    """
    fs, root = fsspec.core.url_to_fs(url)
    remote, local = f"{root.rstrip('/')}/{name}", Path(directory) / name
    if not _exists(fs, remote):
        local.unlink(missing_ok=True)
        return False
    fs.get_file(remote, str(local))
    return True


@contextlib.contextmanager
def lock(url: str, name: str) -> Iterator[None]:
    """
    Hold a lock on the file *name* at *url* while the ``with`` block runs.

    Hold it around reading a file, changing it and writing it back, so a
    second program doing the same waits for the first instead of writing
    over its change. Each hour's pod of a schedule holds it on
    ``releases.csv`` while it copies the file (:func:`get`), adds its hours,
    and copies it back (:func:`put`).

    The lock is a small file ``.<name>.lock`` next to *name*, made only if
    it is not there (``open(..., "x")``: ``If-None-Match`` on S3,
    ``ifGenerationMatch=0`` on Google Cloud Storage, ``O_EXCL`` on a
    folder), and deleted at the end of the block. It says which host and
    process holds it. While the block runs, the holder rewrites it every
    :data:`LOCK_REFRESH_SECONDS`, so a long hold (a pod downloading the met)
    still looks alive. A program that finds it waits, and takes it over if
    it has not changed for :data:`LOCK_STALE_SECONDS` by its own clock, so
    a holder that crashed does not stop every later one.

    Parameters
    ----------
    url : str
        Where the project's inputs are: a URL, or a folder.
    name : str
        The file to lock, relative to the project folder.

    Raises
    ------
    OSError
        If the lock cannot be made or read, such as a refused connection or
        expired credentials.

    Notes
    -----
    A store that ignores ``If-None-Match`` (some older S3-compatible ones)
    lets two programs make the lock at once, so it does not protect there.
    A holder that stops rewriting its lock for :data:`LOCK_STALE_SECONDS`
    (a process paused that long) can be taken over; it then logs a warning
    when it finishes.

    Examples
    --------
    >>> import fsspec
    >>> fs = fsspec.filesystem("memory")
    >>> with lock("memory://lock-example/project", "releases.csv"):
    ...     fs.exists("/lock-example/project/.releases.csv.lock")
    True
    >>> fs.exists("/lock-example/project/.releases.csv.lock")
    False
    """
    fs, root = fsspec.core.url_to_fs(url)
    path = f"{root.rstrip('/')}/.{name}.lock"
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    token = f"{socket.gethostname()} pid {os.getpid()} since {stamp} {uuid.uuid4().hex}"
    try:
        _acquire(fs, path, token)
    except Exception as error:
        raise OSError(f"cannot lock {name} at {url}: {error}") from error
    held = _KeepAlive(fs, path, token)
    held.start()
    try:
        yield
    finally:
        held.stop()
        try:
            if not _release(fs, path, held.token):
                logger.warning(
                    "%s was taken over while this program held it, so another "
                    "may have written %s at the same time.",
                    path,
                    name,
                )
        except Exception:
            # What the block wrote is written; a lock left behind is taken over later.
            logger.warning(
                "Could not remove %s; it is taken over after %d s.",
                path,
                LOCK_STALE_SECONDS,
                exc_info=True,
            )


class _KeepAlive(threading.Thread):
    """
    Rewrite a held lock every :data:`LOCK_REFRESH_SECONDS`, so waiters see it change.

    Each rewrite holds a new token (the first with a count after it);
    :attr:`token` is the latest, which the holder removes at the end. It
    stops when the lock no longer holds its token, which means another
    program took it over.
    """

    def __init__(self, fs: fsspec.AbstractFileSystem, path: str, token: str) -> None:
        super().__init__(name=f"keep {path}", daemon=True)
        self.fs, self.path, self.base, self.token = fs, path, token, token
        self._done = threading.Event()
        self._turn = threading.Lock()

    def run(self) -> None:
        """Rewrite the lock until :meth:`stop`, or until it was taken over."""
        count = 0
        while not self._done.wait(LOCK_REFRESH_SECONDS):
            count += 1
            with self._turn:
                if self._done.is_set():
                    return
                try:
                    if self.fs.read_text(self.path, encoding="utf-8") != self.token:
                        return  # taken over; the holder warns when it finishes
                    token = f"{self.base} refresh {count}"
                    self.fs.pipe_file(self.path, token.encode())
                    self.token = token
                except Exception:
                    # A missed rewrite is retried at the next; waiters take
                    # over only after LOCK_STALE_SECONDS without one.
                    logger.debug("Could not rewrite %s", self.path, exc_info=True)

    def stop(self) -> None:
        """Stop rewriting, and wait for a rewrite under way to end."""
        self._done.set()
        with self._turn:
            pass


def _acquire(fs: fsspec.AbstractFileSystem, path: str, token: str) -> None:
    """
    Make the lock file *path* holding *token*, waiting while another holds it.

    A lock is stale when it reads the same for :data:`LOCK_STALE_SECONDS` on
    this program's own clock (``time.monotonic``), so no two clocks have to
    agree; a stale lock is removed and made again.
    """
    fs.makedirs(path.rpartition("/")[0], exist_ok=True)
    seen, since = None, 0.0
    while True:
        try:
            with fs.open(path, "x") as file:
                file.write(token)
            return
        except FileExistsError:
            pass
        try:
            held = fs.read_text(path, encoding="utf-8")
        except FileNotFoundError:
            continue  # released while we looked: try again
        if held != seen:
            logger.info("Waiting for %s, held by %s", path, held)
            seen, since = held, time.monotonic()
        elif time.monotonic() - since >= LOCK_STALE_SECONDS:
            logger.warning(
                "Taking over %s, the same for %d s: %s", path, LOCK_STALE_SECONDS, held
            )
            _release(fs, path, held)
            seen = None
            continue
        time.sleep(LOCK_POLL_SECONDS)


def _release(fs: fsspec.AbstractFileSystem, path: str, token: str | bytes) -> bool:
    """
    Remove the lock file *path* if it still holds *token*; return whether it did.

    Reading first keeps a program from removing a lock another has taken
    over since. Windows refuses to remove a file while a waiting program has
    it open to read, so a refusal is tried again a few times
    (:data:`LOCK_REMOVE_TRIES`) before it is raised.
    """
    try:
        if fs.read_text(path, encoding="utf-8") != token:
            return False
        for attempt in range(LOCK_REMOVE_TRIES):
            try:
                fs.rm_file(path)
                break
            except PermissionError:
                if attempt == LOCK_REMOVE_TRIES - 1:
                    raise
                time.sleep(LOCK_REMOVE_WAIT_SECONDS)
    except FileNotFoundError:
        return False
    return True


__all__ = [
    "INPUT_FILES",
    "RELEASES",
    "SITES",
    "TABLES",
    "get",
    "has_file",
    "hash_inputs",
    "lock",
    "pull",
    "push",
    "put",
]

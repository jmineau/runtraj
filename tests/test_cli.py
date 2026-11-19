"""Tests for runtraj.cli - Typer command-line interface."""

import errno
import ftplib
import io
import os
import socket
import subprocess
import sys
import urllib.error

import pandas as pd
import pytest
from typer.testing import CliRunner

from runtraj._transient import is_transient
from runtraj.cli import _resolve_project

from .fixtures.factories import make_project_config

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


# ---------------------------------------------------------------------------
# check command
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run command
# ---------------------------------------------------------------------------


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

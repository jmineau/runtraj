"""
An output directory on an object store, against a local S3 server (moto).

The same Output code reads and writes a URL such as ``s3://bucket/output``:
listings go through fsspec, files are put directly, and pyarrow reads
through the store's filesystem.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("s3fs")
moto_server = pytest.importorskip("moto.server")

from runtraj.output import Output  # noqa: E402

from ..fixtures.output import (  # noqa: E402
    VARIANT,
    release_at,
    write_one,
)

BUCKET = "runtraj-test"


@pytest.fixture(scope="module")
def s3_server():
    """A local S3 server for the module, with the credentials and endpoint boto reads."""
    import boto3
    import s3fs

    server = moto_server.ThreadedMotoServer(port=0, verbose=False)
    server.start()
    _, port = server.get_host_and_port()
    names = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION")
    saved = {k: os.environ.get(k) for k in (*names, "AWS_ENDPOINT_URL")}
    os.environ.update(dict.fromkeys(names[:2], "testing"))
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["AWS_ENDPOINT_URL"] = f"http://127.0.0.1:{port}"
    s3fs.S3FileSystem.clear_instance_cache()
    boto3.client("s3").create_bucket(Bucket=BUCKET)
    yield
    server.stop()
    s3fs.S3FileSystem.clear_instance_cache()
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def store(s3_server) -> str:
    """A fresh folder on the S3 server."""
    return f"s3://{BUCKET}/{uuid.uuid4().hex}"


def test_logs_and_failure_records_on_an_object_store(store):
    out = Output(f"{store}/output")
    rid = str(release_at().id)
    log = out.write_log(VARIANT, rid, "hycs_std ran\n")
    assert log.read_text() == "hycs_std ran\n"
    record = {"stage": "particles", "reason": "MET_COVERAGE", "message": "boom"}
    out.record_failure("particles", VARIANT, rid, record)
    assert out.failure("particles", VARIANT, rid) == record
    out.clear_failure("particles", VARIANT, rid)
    assert out.failure("particles", VARIANT, rid) is None


def test_a_kept_workdir_on_an_object_store_leaves_out_links(store, tmp_path):
    out = Output(f"{store}/output")
    rid = str(release_at().id)
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "CONTROL").write_text("x")
    (workdir / "met").symlink_to(tmp_path)  # HYSPLIT links its met files

    kept = out.keep_workdir(VARIANT, rid, workdir)

    assert kept == out.kept_workdir(VARIANT, rid)
    assert (kept / "CONTROL").read_text() == "x"
    assert not (kept / "met").exists()


def test_results_read_back_from_an_object_store_as_from_a_folder(store, tmp_path):
    """``table`` and ``read`` take their own branch on a bucket (``filesystem=``)."""
    local, bucket = Output(tmp_path / "output"), Output(f"{store}/output")
    # Two days, so the date partition is read from the folder names.
    releases = [release_at(12, 15), release_at(6, 16)]
    for out in (local, bucket):
        for release in releases:
            write_one(out, release=release)
    ids = [str(r.id) for r in releases]

    table = bucket.table("particles", VARIANT, ids)

    assert table.num_rows == 1000
    assert table.equals(local.table("particles", VARIANT, ids))
    assert table["date"].unique().to_pylist() == [r.time.date() for r in releases]
    assert bucket.read("particles", VARIANT, ids[1]).equals(
        local.read("particles", VARIANT, ids[1])
    )
    assert bucket.table("particles", VARIANT, columns=["lon"]).equals(
        local.table("particles", VARIANT, columns=["lon"])
    )

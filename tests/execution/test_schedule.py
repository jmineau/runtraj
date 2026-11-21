"""Tests for running a project every hour: the due hours, sites.csv, the lock on releases.csv, and the CronJob."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import fsspec
import pytest
import yaml

from runtraj.execution import inputs, kubernetes, schedule
from runtraj.execution.config import ExecutionConfig
from runtraj.execution.inputs import pull, push, put
from runtraj.project import Project

from ..fixtures.factories import make_project_config

IMAGE = "registry.example.org/runtraj-hysplit:1"
NOW = datetime(2026, 10, 29, 18, 15)
SITES = (
    "name,longitude,latitude,altitude,altitude_ref\n"
    "tower,-111.8476,40.7663,35,agl\n"
    "valley,-112.1155,40.5385,5,\n"
)


def _project(tmp_path: Path, output: str | None = None) -> Project:
    """A project with no releases yet and two sites, its output in ``tmp_path / "out"`` unless given."""
    project = Project.init(tmp_path / "towers", config=make_project_config(tmp_path))
    (project.directory / "sites.csv").write_text(SITES)
    return Project(project.directory, output=output or str(tmp_path / "out"))


@pytest.fixture
def bucket() -> str:
    """A fresh ``memory://`` folder for one test's inputs."""
    return f"memory://schedule-{uuid.uuid4().hex}/projects/towers"


def _rows(location: str | Path) -> list[str]:
    """The data lines of ``releases.csv`` at *location*."""
    fs, root = fsspec.core.url_to_fs(str(location))
    return fs.cat_file(f"{root}/releases.csv").decode().splitlines()[1:]


def _times(location: str | Path) -> list[str]:
    """The ``time`` of each row of ``releases.csv`` at *location*, as written."""
    fs, root = fsspec.core.url_to_fs(str(location))
    lines = fs.cat_file(f"{root}/releases.csv").decode().splitlines()
    column = lines[0].split(",").index("time")
    return [line.split(",")[column] for line in lines[1:]]


def _tick(url: str, pod: Path, now: datetime = NOW, delay: int = 8) -> list[str]:
    """What a tick's pod does with the inputs on *url*: copy them to *pod*, add the due releases."""
    return schedule.add_due_releases(url, pull(url, pod), now=now, delay=delay)


# ---------------------------------------------------------------------------
# The due hours
# ---------------------------------------------------------------------------


def test_due_hours_are_the_last_day_behind_the_delay():
    hours = schedule.due_hours(NOW, delay=8)
    assert len(hours) == schedule.WINDOW_HOURS
    assert hours[-1] == datetime(2026, 10, 29, 10)
    assert hours[0] == datetime(2026, 10, 28, 11)
    assert schedule.due_hours(NOW, delay=0)[-1] == datetime(2026, 10, 29, 18)


def test_due_hours_without_since_leave_a_gap_after_an_outage():
    before = schedule.due_hours(datetime(2021, 1, 15, 12, 0), delay=8)
    after = schedule.due_hours(datetime(2021, 1, 17, 0, 0), delay=8)
    assert after[0] - before[-1] > timedelta(hours=1)


def test_due_hours_with_since_close_the_gap():
    before = schedule.due_hours(datetime(2021, 1, 15, 12, 0), delay=8)
    after = schedule.due_hours(datetime(2021, 1, 17, 0, 0), delay=8, since=before[-1])
    assert after[0] == before[-1] + timedelta(hours=1)


@pytest.mark.parametrize("back", [2, 25])
def test_due_hours_ignore_a_since_inside_or_just_before_the_window(back):
    hours = schedule.due_hours(NOW, delay=8)
    since = hours[-1] - timedelta(hours=back - 1)
    assert schedule.due_hours(NOW, delay=8, since=since) == hours


def test_due_hours_reach_back_at_most_a_week_more():
    normal = schedule.due_hours(NOW, delay=8)
    hours = schedule.due_hours(NOW, delay=8, since=normal[0] - timedelta(days=365))
    assert hours[0] == normal[0] - timedelta(hours=schedule.MAX_BACKFILL_HOURS)
    assert hours[-1] == normal[-1]


def test_a_now_later_than_the_clock_is_refused():
    with pytest.raises(ValueError, match="later than the clock"):
        schedule.check_now(datetime.now(UTC) + timedelta(hours=2))


def test_a_now_with_a_time_zone_is_read_in_utc():
    eastern = datetime(2026, 10, 29, 14, 15, tzinfo=timezone(timedelta(hours=-4)))
    assert schedule.check_now(eastern) == NOW


# ---------------------------------------------------------------------------
# sites.csv
# ---------------------------------------------------------------------------


def test_read_sites(tmp_path):
    assert schedule.read_sites(_project(tmp_path).directory) == [
        {
            "longitude": -111.8476,
            "latitude": 40.7663,
            "altitude": 35.0,
            "altitude_ref": "agl",
        },
        {"longitude": -112.1155, "latitude": 40.5385, "altitude": 5.0},
    ]


def test_read_sites_without_the_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="has no sites.csv"):
        schedule.read_sites(tmp_path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("longitude,latitude\n-111.8,40.7\n", "needs rows with columns"),
        ("longitude,latitude,altitude\n", "needs rows with columns"),
        (
            "lon,lat,alt\n-111.8,40.7,35\n",
            r"needs rows with columns longitude, latitude, altitude; "
            r"its columns are 'lon', 'lat', 'alt' and it has 1 rows",
        ),
        ("longitude,latitude,altitude\nwest,40.7,5\n", "line 2: .*not all numbers"),
        ("longitude,latitude,altitude\n-111.8,40.7\n", r"line 2: no altitude\."),
        ("longitude,latitude,altitude\n-111.8,nan,35\n", "line 2: not all numbers"),
        (
            "longitude,latitude,altitude\n-111.8,40.7,35\n-111.8,95,35\n",
            r"line 3: latitude must be a number within \[-90, 90\]",
        ),
        (
            "longitude,latitude,altitude,altitude_ref\n-111.8,40.7,35,above\n",
            "line 2: .*agl.*msl",
        ),
    ],
)
def test_read_sites_names_the_line_it_refuses(tmp_path, text, message):
    (tmp_path / "sites.csv").write_text(text)
    with pytest.raises(ValueError, match=f"sites.csv {message}"):
        schedule.read_sites(tmp_path)


def test_read_sites_keeps_a_repeated_site_once_and_says_so(tmp_path, caplog):
    (tmp_path / "sites.csv").write_text(
        "name,longitude,latitude,altitude\n"
        "tower,-111.8,40.7,35\n"
        "valley,-112.1,40.5,5\n"
        "tower again,-111.8,40.7,35\n"
    )

    sites = schedule.read_sites(tmp_path)

    assert len(sites) == 2
    assert "sites.csv line 4 repeats line 2" in caplog.text


def test_the_same_place_at_two_altitudes_is_two_sites(tmp_path, caplog):
    (tmp_path / "sites.csv").write_text(
        "longitude,latitude,altitude\n-111.8,40.7,35\n-111.8,40.7,125\n"
    )

    assert len(schedule.read_sites(tmp_path)) == 2
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_read_sites_takes_a_file_saved_with_a_byte_order_mark(tmp_path):
    """Excel's "CSV UTF-8" puts one before the first column name."""
    (tmp_path / "sites.csv").write_text(
        "longitude,latitude,altitude\n-111.8,40.7,35\n", encoding="utf-8-sig"
    )
    assert schedule.read_sites(tmp_path) == [
        {"longitude": -111.8, "latitude": 40.7, "altitude": 35.0}
    ]


def test_hourly_releases_are_each_site_each_hour(tmp_path):
    sites = schedule.read_sites(_project(tmp_path).directory)
    releases = schedule.hourly_releases(sites, schedule.due_hours(NOW)[-2:])
    assert [r.id for r in releases] == [
        "202610290900_-111.8476_40.7663_35",
        "202610290900_-112.1155_40.5385_5",
        "202610291000_-111.8476_40.7663_35",
        "202610291000_-112.1155_40.5385_5",
    ]


# ---------------------------------------------------------------------------
# Adding the due releases
# ---------------------------------------------------------------------------


def test_push_and_pull_carry_sites_and_a_project_with_no_releases_yet(tmp_path, bucket):
    project = _project(tmp_path)
    assert push(project.directory, bucket, releases=False) == [
        "config.yaml",
        "sites.csv",
    ]
    pod = pull(bucket, tmp_path / "pod")
    assert (pod / "sites.csv").read_text() == SITES
    assert not (pod / "releases.csv").exists()


def test_push_says_which_files_the_bucket_has_that_the_project_no_longer_has(
    tmp_path, bucket, caplog
):
    project = _project(tmp_path)
    (project.directory / "tables").mkdir()
    (project.directory / "tables" / "old.parquet").write_text("x")
    push(project.directory, bucket, releases=False)
    caplog.clear()

    (project.directory / "sites.csv").unlink()
    (project.directory / "tables" / "old.parquet").unlink()
    push(project.directory, bucket, releases=False)

    assert "sites.csv, tables/old.parquet, which this project does not" in caplog.text
    # It says, and leaves them: a push never deletes.
    fs, root = fsspec.core.url_to_fs(bucket)
    assert fs.exists(f"{root}/sites.csv")


def test_push_of_a_project_that_matches_the_bucket_says_nothing(
    tmp_path, bucket, caplog
):
    project = _project(tmp_path)
    push(project.directory, bucket, releases=False)
    push(project.directory, bucket, releases=False)

    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_a_tick_adds_the_last_day_at_each_site_and_puts_it_back(tmp_path, bucket):
    push(_project(tmp_path).directory, bucket, releases=False)

    ids = _tick(bucket, tmp_path / "pod")

    assert len(ids) == 2 * schedule.WINDOW_HOURS
    assert ids[-1] == "202610291000_-112.1155_40.5385_5"
    assert len(_rows(bucket)) == 2 * schedule.WINDOW_HOURS


def test_the_next_tick_adds_only_the_new_hour(tmp_path, bucket):
    push(_project(tmp_path).directory, bucket, releases=False)
    _tick(bucket, tmp_path / "first")

    ids = _tick(bucket, tmp_path / "second", now=NOW.replace(hour=19))

    assert len(_rows(bucket)) == 2 * (schedule.WINDOW_HOURS + 1)
    assert ids[0].startswith("202610281200") and ids[-1].startswith("202610291100")


def test_a_tick_after_an_outage_adds_and_runs_the_gap(tmp_path, bucket):
    push(_project(tmp_path).directory, bucket, releases=False)
    _tick(bucket, tmp_path / "first")

    ids = _tick(bucket, tmp_path / "second", now=NOW + timedelta(hours=36))

    hours = sorted(set(_times(bucket)))
    assert (hours[0], hours[-1], len(hours)) == (
        "2026-10-28 11:00:00",
        "2026-10-30 22:00:00",
        60,
    )
    assert len(ids) == 2 * 36  # the gap runs in this tick too
    assert ids[0] == "202610291100_-111.8476_40.7663_35"


def test_a_tick_reads_the_bucket_s_releases_not_an_old_copy(tmp_path, bucket):
    """The pod's copy was pulled before the lock; the bucket's is read under it."""
    push(_project(tmp_path).directory, bucket, releases=False)
    pod = pull(bucket, tmp_path / "pod")
    _tick(bucket, tmp_path / "other")  # another tick adds the day meanwhile

    schedule.add_due_releases(bucket, pod, now=NOW.replace(hour=19), delay=8)

    assert len(_rows(bucket)) == 2 * (schedule.WINDOW_HOURS + 1)


def test_releases_in_the_future_do_not_hide_a_gap(tmp_path, bucket):
    project = _project(tmp_path)
    (project.directory / "releases.csv").write_text(
        "time,longitude,latitude,altitude\n"
        "2026-10-28 10:00:00,-111.8476,40.7663,35\n"
        "2030-01-01 00:00:00,-111.8476,40.7663,35\n"
    )
    push(project.directory, bucket)

    ids = _tick(bucket, tmp_path / "pod", now=NOW + timedelta(hours=36))

    assert ids[0].startswith("202610281100")


def test_a_tick_on_a_project_folder_adds_to_it_in_place(tmp_path):
    project = _project(tmp_path)

    schedule.add_due_releases(
        str(project.directory), project.directory, now=NOW, delay=0
    )

    times = _times(project.directory)
    assert len(times) == 2 * schedule.WINDOW_HOURS
    assert times[-1] == "2026-10-29 18:00:00"
    assert not (project.directory / ".releases.csv.lock").exists()


def test_a_tick_needs_sites(tmp_path):
    project = Project.init(tmp_path / "p", config=make_project_config(tmp_path))
    with pytest.raises(FileNotFoundError, match="sites.csv"):
        schedule.add_due_releases(str(project.directory), project.directory, now=NOW)


def test_overlapping_ticks_keep_both_ticks_releases(tmp_path, bucket, monkeypatch):
    """A tick run by hand while the hourly one is between its copy and its put waits for it."""
    push(_project(tmp_path).directory, bucket, releases=False)
    monkeypatch.setattr(inputs, "LOCK_POLL_SECONDS", 0.01)
    paused, resume = threading.Event(), threading.Event()

    def slow_put(*args):
        if not paused.is_set():
            paused.set()
            resume.wait(10)
        put(*args)

    monkeypatch.setattr(schedule, "put", slow_put)
    errors: list[Exception] = []

    def tick(now: datetime, pod: str) -> None:
        try:
            _tick(bucket, tmp_path / pod, now=now)
        except Exception as error:
            errors.append(error)

    hourly = threading.Thread(target=tick, args=(NOW, "hourly"))
    hourly.start()
    assert paused.wait(10)
    by_hand = threading.Thread(target=tick, args=(NOW.replace(day=1), "by-hand"))
    by_hand.start()
    by_hand.join(0.5)
    assert by_hand.is_alive()  # waiting for the hourly tick's lock
    resume.set()
    hourly.join(10)
    by_hand.join(10)
    assert errors == []
    assert len(_rows(bucket)) == 2 * 2 * schedule.WINDOW_HOURS
    fs, root = fsspec.core.url_to_fs(bucket)
    assert not fs.exists(f"{root}/.releases.csv.lock")


# ---------------------------------------------------------------------------
# The lock, and writing a file back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["bucket", "folder"])
def test_the_lock_lets_one_holder_in_at_a_time(where, bucket, tmp_path, monkeypatch):
    monkeypatch.setattr(inputs, "LOCK_POLL_SECONDS", 0.01)
    url = bucket if where == "bucket" else str(tmp_path)
    fs, root = fsspec.core.url_to_fs(url)
    order = []

    def second():
        with inputs.lock(url, "releases.csv"):
            order.append("second")

    with inputs.lock(url, "releases.csv"):
        assert fs.exists(f"{root}/.releases.csv.lock")
        thread = threading.Thread(target=second)
        thread.start()
        thread.join(0.2)
        assert thread.is_alive()
        order.append("first")
    thread.join(10)
    assert order == ["first", "second"]
    assert not fs.exists(f"{root}/.releases.csv.lock")


def test_the_lock_is_let_go_when_the_block_raises(bucket):
    fs, root = fsspec.core.url_to_fs(bucket)
    with (
        pytest.raises(ValueError, match="bad row"),
        inputs.lock(bucket, "releases.csv"),
    ):
        raise ValueError("bad row")
    assert not fs.exists(f"{root}/.releases.csv.lock")


def test_a_stale_lock_is_taken_over(bucket, monkeypatch, caplog):
    """A holder that crashed leaves its lock; the next one takes it over."""
    monkeypatch.setattr(inputs, "LOCK_POLL_SECONDS", 0.01)
    monkeypatch.setattr(inputs, "LOCK_STALE_SECONDS", 0.05)
    fs, root = fsspec.core.url_to_fs(bucket)
    fs.pipe_file(f"{root}/.releases.csv.lock", b"a pod that crashed")
    with inputs.lock(bucket, "releases.csv"):
        assert fs.cat_file(f"{root}/.releases.csv.lock") != b"a pod that crashed"
    assert "Taking over" in caplog.text
    assert not fs.exists(f"{root}/.releases.csv.lock")


def test_a_lock_taken_over_is_left_to_its_new_holder(bucket, caplog):
    fs, root = fsspec.core.url_to_fs(bucket)
    with inputs.lock(bucket, "releases.csv"):
        fs.pipe_file(f"{root}/.releases.csv.lock", b"the one that took over")
    assert fs.cat_file(f"{root}/.releases.csv.lock") == b"the one that took over"
    assert "was taken over" in caplog.text


def test_a_lock_that_cannot_be_made_is_an_os_error(bucket, monkeypatch):
    def refuse(*args):
        raise RuntimeError("the store went away")

    monkeypatch.setattr(inputs, "_acquire", refuse)
    with (
        pytest.raises(
            OSError, match=r"cannot lock releases.csv at memory://.*went away"
        ),
        inputs.lock(bucket, "releases.csv"),
    ):
        pass


def test_put_on_a_folder_is_never_seen_half_written(tmp_path):
    """Every read gets the whole file: ``put`` replaces it, never truncates it."""
    pod, shared = tmp_path / "pod", tmp_path / "shared"
    pod.mkdir()
    shared.mkdir()
    content = b"time,longitude,latitude,altitude\n" + b"x" * 50_000 + b"\n"
    (pod / "releases.csv").write_bytes(content)
    path = shared / "releases.csv"
    path.write_bytes(content)
    stop = threading.Event()
    torn: list[int] = []

    def read_forever():
        while not stop.is_set():
            data = path.read_bytes()
            if data != content:
                torn.append(len(data))

    reader = threading.Thread(target=read_forever)
    reader.start()
    try:
        for _ in range(200):
            put(pod, str(shared), "releases.csv")
    finally:
        stop.set()
        reader.join()
    assert torn == []


# ---------------------------------------------------------------------------
# The CronJob
# ---------------------------------------------------------------------------


def _execution(**kube) -> ExecutionConfig:
    return ExecutionConfig(backend="kubernetes", kubernetes={"image": IMAGE, **kube})


def test_the_cron_job_runs_one_tick_an_hour_at_a_time():
    cron = kubernetes.cron_job_manifest(
        "Towers",
        "s3://lab/projects/towers",
        _execution(cpus=4, memory="8Gi", namespace="runs"),
        output="s3://lab/out",
        delay=6,
    )
    spec = cron["spec"]
    assert cron["kind"] == "CronJob"
    assert cron["metadata"]["name"] == kubernetes.cron_job_name(
        "Towers", "s3://lab/projects/towers"
    )
    assert cron["metadata"]["namespace"] == "runs"
    assert (spec["schedule"], spec["timeZone"]) == (kubernetes.SCHEDULE, "Etc/UTC")
    assert spec["concurrencyPolicy"] == "Forbid"
    job = spec["jobTemplate"]
    assert job["metadata"]["labels"][kubernetes.PROJECT_LABEL] == "towers"
    assert (job["spec"]["completions"], job["spec"]["parallelism"]) == (1, 1)
    assert job["spec"]["activeDeadlineSeconds"] == kubernetes.TICK_DEADLINE
    [container] = job["spec"]["template"]["spec"]["containers"]
    assert container["args"] == [
        "runtraj",
        "tick",
        "s3://lab/projects/towers",
        "--delay",
        "6",
        "--cpus",
        "4",
        "--output",
        "s3://lab/out",
    ]
    assert container["resources"]["limits"] == {"memory": "8Gi"}
    rules = job["spec"]["podFailurePolicy"]["rules"]
    assert {"FailJob", "FailIndex", "Ignore"} == {r["action"] for r in rules}


def test_the_cron_job_keeps_a_deadline_the_block_sets():
    cron = kubernetes.cron_job_manifest(
        "t", "/p", _execution(active_deadline_seconds=600), output="/out"
    )
    assert cron["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 600


def test_two_projects_of_one_folder_name_get_two_cron_jobs():
    a = kubernetes.cron_job_name("winter", "s3://lab-a/projects/winter")
    b = kubernetes.cron_job_name("winter", "s3://lab-b/runs/winter")
    assert a != b
    assert a == kubernetes.cron_job_name("winter", "S3://lab-a/projects/winter/")
    long = kubernetes.cron_job_name("x" * 80, "/p")
    assert len(long) <= 52


@pytest.fixture
def kubectl(monkeypatch):
    """Record the manifests given to ``kubectl apply``."""
    applied: list[dict] = []

    def fake_run(args, *, input, **kwargs):
        assert args[1:] == ["apply", "-f", "-", "-o", "name"]
        manifest = yaml.safe_load(input)
        applied.append(manifest)
        name = manifest["metadata"]["name"]
        return SimpleNamespace(
            returncode=0, stdout=f"cronjob.batch/{name}\n", stderr=""
        )

    monkeypatch.setattr(kubernetes.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(kubernetes.subprocess, "run", fake_run)
    return applied


def test_schedule_copies_the_inputs_and_applies_the_cron_job(tmp_path, bucket, kubectl):
    project = _project(tmp_path, output="memory://schedule-out/out")

    name = kubernetes.schedule_job(project, _execution(inputs=bucket))

    [cron] = kubectl
    assert name == cron["metadata"]["name"]
    args = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0][
        "args"
    ]
    assert args[2] == bucket
    fs, root = fsspec.core.url_to_fs(bucket)
    assert fs.isfile(f"{root}/sites.csv")
    [folder] = (project.directory / "_kubernetes").iterdir()
    assert yaml.safe_load((folder / "cronjob.yaml").read_text()) == cron
    assert (folder / "cronjob.name").read_text() == f"{name}\n"


def test_scheduling_again_leaves_the_bucket_s_releases_alone(tmp_path, bucket, kubectl):
    """Each hour's pod adds to the bucket's releases.csv; a push would write over its hours."""
    project = _project(tmp_path, output="memory://schedule-out/out")
    kubernetes.schedule_job(project, _execution(inputs=bucket))
    _tick(bucket, tmp_path / "pod")
    project.add_releases(
        schedule.hourly_releases(
            schedule.read_sites(project.directory), [NOW.replace(minute=0)]
        )
    )

    kubernetes.schedule_job(project, _execution(inputs=bucket))

    assert len(_rows(bucket)) == 2 * schedule.WINDOW_HOURS


def test_schedule_needs_sites(tmp_path, kubectl):
    project = Project.init(tmp_path / "p", config=make_project_config(tmp_path))
    with pytest.raises(FileNotFoundError, match="sites.csv"):
        kubernetes.schedule_job(project, _execution())
    assert kubectl == []


def test_schedule_refuses_inputs_on_a_bucket_with_results_on_a_pod_s_disk(
    tmp_path, bucket, kubectl
):
    with pytest.raises(ValueError, match="each pod would write its results"):
        kubernetes.schedule_job(_project(tmp_path), _execution(inputs=bucket))
    assert kubectl == []

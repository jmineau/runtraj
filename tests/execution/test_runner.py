"""Tests for runtraj.execution.runner: shares of the releases, submission, and the fetch before a run."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from runtraj.execution import runner, slurm
from runtraj.execution.config import (
    ExecutionConfig,
    FetchConfig,
    SlurmConfig,
    slurm_minutes,
)
from runtraj.execution.runner import _project_slug, task_share
from runtraj.project import Project
from tests.fixtures.platform import not_on_windows

from ..fixtures.factories import make_met_config, make_project_config
from ..fixtures.slurm import hourly_project as _hourly_project
from ..fixtures.slurm import out as _out
from ..fixtures.slurm import sbatch_options as _sbatch

# ---------------------------------------------------------------------------
# Submitting to Slurm
# ---------------------------------------------------------------------------


@pytest.fixture
def pending(monkeypatch):
    """Set which releases the runner finds incomplete."""
    ids: list[str] = []
    monkeypatch.setattr(runner, "pending", lambda project, **_: list(ids))
    return ids


def test_submit_writes_a_submission_and_hands_it_to_sbatch(pending, commands, tmp_path):
    import pandas as pd

    project = _hourly_project(tmp_path, 4)
    ids = list(project.releases["release"])
    pending.extend([ids[2], ids[0], ids[3]])  # the order a run gives
    execution = ExecutionConfig(
        backend="slurm",
        timeout=600,
        slurm={"time": "01:00:00", "n_workers": 2},
    )
    commands.answers["sbatch"] = [_out("777;cluster\n")]

    job_id = runner.submit(project, execution=execution, skip_existing=False)

    assert job_id == "777"
    [(sbatch, parsable, script)] = commands.ran
    assert (sbatch, parsable) == ("sbatch", "--parsable")
    folder = Path(script).parent
    assert folder.parent == project.directory / "_slurm"
    assert (folder / "releases.txt").read_text().split() == pending
    # The tasks read the checked rows, in the order they split them.
    rows = pd.read_parquet(folder / "releases.parquet")
    assert rows["release"].tolist() == pending
    assert {"kind", "location", "altitude_ref"} <= set(rows.columns)
    stored = yaml.safe_load((folder / "execution.yaml").read_text())
    assert ExecutionConfig.model_validate(stored) == execution
    text = Path(script).read_text(encoding="utf-8")
    # Three releases, so three tasks (ten per worker would be 20), two at once.
    assert _sbatch(text)["array"] == "0-2%2"
    assert "--no-skip" in text


def test_submit_uses_no_more_tasks_than_releases(pending, commands, tmp_path):
    project = _hourly_project(tmp_path, 1)
    pending.extend(project.releases["release"])
    commands.answers["sbatch"] = [_out("5\n")]
    runner.submit(
        project,
        execution=ExecutionConfig(
            backend="slurm", slurm={"time": "01:00:00", "n_workers": 8}
        ),
    )
    assert (
        _sbatch(Path(commands.ran[0][2]).read_text(encoding="utf-8"))["array"] == "0-0"
    )


def test_submit_with_nothing_to_do_submits_nothing(pending, commands, tmp_path):
    assert (
        runner.submit(
            _hourly_project(tmp_path, 1),
            execution=ExecutionConfig(backend="slurm", slurm={"time": "01:00:00"}),
        )
        is None
    )
    assert commands.ran == []


def test_submit_says_why_sbatch_refused(pending, commands, tmp_path):
    project = _hourly_project(tmp_path, 1)
    pending.extend(project.releases["release"])
    commands.answers["sbatch"] = [_out(returncode=1, stderr="Invalid account")]
    with pytest.raises(RuntimeError, match="Invalid account"):
        runner.submit(
            project,
            execution=ExecutionConfig(backend="slurm", slurm={"time": "01:00:00"}),
        )


def test_waiting_stops_when_sacct_says_nothing_for_too_long(commands, monkeypatch):
    """On a cluster without the accounting database, sacct never answers."""
    monkeypatch.setattr(slurm, "SILENT_SCHEDULER_SECONDS", 0)
    commands.answers["sacct"] = [
        _out(returncode=1, stderr="Slurm accounting storage is disabled")
    ]
    with pytest.raises(RuntimeError, match="nothing about job 9 for 0 minutes"):
        slurm.wait("9", poll=0)


def test_waiting_polls_sacct_until_every_task_ends(commands, caplog):
    commands.answers["sacct"] = [
        _out(returncode=1),  # the accounting database has not heard of it yet
        _out("9_0|RUNNING|0:0\n9_[1-3]|PENDING|0:0\n"),
        _out("9_0|COMPLETED|0:0\n9_1|REQUEUED|0:0\n9_2|RUNNING|0:0\n9_3|RUNNING|0:0\n"),
        _out(
            "9_0|COMPLETED|0:0\n9_1|FAILED|1:0\n9_2|OUT_OF_MEMORY|0:125\n"
            "9_3|CANCELLED by 123|0:15\n"
        ),
    ]
    with caplog.at_level("WARNING", logger="runtraj.execution.runner"):
        states = slurm.wait("9", poll=0)

    assert [c[0] for c in commands.ran] == ["sacct"] * 4
    warned = caplog.text
    assert "9_2 ended OUT_OF_MEMORY" in warned
    assert "9_3 ended CANCELLED" in warned
    assert "9_1" not in warned  # failed simulations are the status table's to report
    assert states["9_2"] == ("OUT_OF_MEMORY", "0:125")
    assert "9_0" not in warned


def test_a_job_on_another_cluster_is_followed_there(
    pending, commands, tmp_path, monkeypatch
):
    """``sbatch --parsable`` names the cluster when it is not the default."""
    import pandas as pd

    project = _hourly_project(tmp_path, 2)
    pending.extend(project.releases["release"])
    monkeypatch.setattr(
        runner, "_status", lambda project, ids: pd.DataFrame({"state": ["complete"]})
    )
    commands.answers["sbatch"] = [_out("777;kingspeak\n")]
    commands.answers["sacct"] = [
        _out("777_0|RUNNING|0:0\n"),
        _out("777_0|COMPLETED|0:0\n"),
    ]
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    execution = ExecutionConfig(
        backend="slurm", slurm={"time": "01:00:00", "clusters": "kingspeak"}
    )

    runner.run(project, execution=execution)

    sacct = [c for c in commands.ran if c[0] == "sacct"]
    assert len(sacct) == 2
    assert all(c[1:5] == ["-M", "kingspeak", "-j", "777"] for c in sacct)


def test_a_run_lists_once_more_when_the_files_look_late(
    pending, commands, tmp_path, monkeypatch
):
    """The submitting node can see new files a minute late on a network filesystem."""
    import pandas as pd

    project = _hourly_project(tmp_path, 1)
    pending.extend(project.releases["release"])
    listings = iter([["pending"], ["complete"]])
    monkeypatch.setattr(
        runner, "_status", lambda project, ids: pd.DataFrame({"state": next(listings)})
    )
    monkeypatch.setattr(slurm, "wait", lambda job_id, cluster=None: {})
    slept: list[float] = []
    monkeypatch.setattr(runner.time, "sleep", slept.append)
    commands.answers["sbatch"] = [_out("5\n")]

    table = runner.run(
        project, execution=ExecutionConfig(backend="slurm", slurm={"time": "01:00:00"})
    )

    assert slept == [runner.RELIST_SECONDS]
    assert table["state"].tolist() == ["complete"]


def test_a_job_on_the_default_cluster_names_none(commands):
    commands.answers["sacct"] = [_out("9_0|COMPLETED|0:0\n")]
    slurm.wait("9", poll=0)
    assert commands.ran[0][:3] == ["sacct", "-j", "9"]


# ---------------------------------------------------------------------------
# Time limits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("time", "minutes"),
    [
        pytest.param(90, 90, id="90-as-int"),
        ("90", 90),
        ("30:00", 30),
        ("02:00:00", 120),
        ("00:10:30", 11),  # rounded up
        ("1-00", 1440),
        ("1-12:30", 2190),
        ("2-00:00:00", 2880),
    ],
)
def test_time_limits_are_read_the_way_sbatch_reads_them(time, minutes):
    assert slurm_minutes(time) == minutes


@pytest.mark.parametrize("time", ["soon", "1:2:3:4", "-5", "0", "00:00:00", "1-"])
def test_a_time_limit_sbatch_would_not_take_is_an_error(time):
    with pytest.raises(ValueError, match="time limit"):
        ExecutionConfig(slurm={"time": time})


def test_a_slurm_run_needs_a_time_limit():
    with pytest.raises(ValueError, match="execution.slurm.time is required"):
        ExecutionConfig(backend="slurm")
    ExecutionConfig()  # a local run has none
    ExecutionConfig(backend="slurm", slurm={"time": "1-00"})
    # A time written as null is no time limit, not a bad one.
    assert SlurmConfig(time=None, fetch={"time": None}).time is None


@pytest.mark.parametrize(
    ("directory", "expected"),
    [
        ("/data/projects/My_Project", "my-project"),
        ("/data/projects/My_Project/", "my-project"),
        ("/data/Weird  Name!!", "weird-name"),
        ("", "project"),
    ],
)
def test_project_slug_names_the_slurm_job(directory, expected):
    assert _project_slug(directory) == expected


# ---------------------------------------------------------------------------
# A share of the releases: --releases and --task
# ---------------------------------------------------------------------------


def test_task_share_is_every_nth_release():
    ids = list("abcdefg")
    assert [task_share(ids, i, 3) for i in range(3)] == [
        ["a", "d", "g"],
        ["b", "e"],
        ["c", "f"],
    ]
    assert task_share(ids[:1], 2, 3) == []
    for task, n in [(3, 3), (-1, 3), (0, 0)]:
        with pytest.raises(ValueError, match="0 to n_tasks - 1"):
            task_share(ids, task, n)


def test_tasks_split_the_releases_the_same_way_whenever_each_starts(
    tmp_path, monkeypatch
):
    """
    A share is taken before the complete releases are dropped.

    Taken after, a task that starts late would shift onto another task's
    releases.
    """
    project = _hourly_project(tmp_path, 7)
    every = sorted(set(project.simulations["release"]), key=runner._scattered)
    done: set[str] = set()
    monkeypatch.setattr(
        Project, "incomplete", lambda self, sel=None: sel[~sel["release"].isin(done)]
    )

    first = runner.pending(project, task=(0, 3))
    done.update(every[:4])  # some finish before tasks 1 and 2 start
    later = [runner.pending(project, task=(i, 3)) for i in (1, 2)]

    shares = [every[i::3] for i in range(3)]
    assert first == shares[0]
    assert later == [[r for r in shares[i] if r not in done] for i in (1, 2)]


def test_releases_left_at_one_stride_of_the_project_spread_over_the_tasks(
    tmp_path, monkeypatch
):
    """One site's hours among many: in project order every one fell to the same task."""
    project = _hourly_project(tmp_path, 24)
    every = list(dict.fromkeys(project.simulations["release"]))
    left = set(every[::4])  # 6 releases, one every 4th
    monkeypatch.setattr(
        Project, "incomplete", lambda self, sel=None: sel[sel["release"].isin(left)]
    )

    shares = [runner.pending(project, task=(i, 4)) for i in range(4)]

    assert sorted(r for share in shares for r in share) == sorted(left)
    assert max(len(share) for share in shares) < len(left)


def test_a_release_list_limits_the_run_and_keeps_its_order(tmp_path, monkeypatch):
    project = _hourly_project(tmp_path, 4)
    every = list(dict.fromkeys(project.simulations["release"]))
    monkeypatch.setattr(
        Project, "incomplete", lambda self, sel=None: sel[sel["release"] != every[3]]
    )
    listed = [every[3], every[1], every[1], every[0]]
    assert runner.pending(project, releases=listed) == [every[1], every[0]]
    assert runner.pending(project, releases=listed, skip_existing=False) == [
        every[3],
        every[1],
        every[0],
    ]
    assert runner.pending(
        project, releases=listed, task=(1, 2), skip_existing=False
    ) == [every[1]]
    with pytest.raises(ValueError, match="not in this project"):
        runner.pending(project, releases=["nope", every[0]])


def _look_alike_project(tmp_path) -> Project:
    """A project of two mets whose files carry one source id, without labels."""
    config = make_project_config(
        tmp_path,
        mets={
            "wrf-mynn": make_met_config(tmp_path / "mynn"),
            "wrf-ysu": make_met_config(tmp_path / "ysu"),
        },
        variants={"mynn": {"met": "wrf-mynn"}, "ysu": {"met": "wrf-ysu"}},
    )
    return Project.init(tmp_path / "proj", config=config)


def test_a_run_refuses_two_mets_that_look_alike(tmp_path, monkeypatch):
    project = _look_alike_project(tmp_path)
    monkeypatch.setattr(
        runner, "pending", lambda *a, **k: pytest.fail("listed the runs first")
    )

    with pytest.raises(ValueError, match="label: wrf-mynn"):
        runner.run(project)
    with pytest.raises(ValueError, match="label: wrf-mynn"):
        runner.run(project, task=(0, 2))
    with pytest.raises(ValueError, match="label: wrf-mynn"):
        runner.submit(
            project,
            execution=ExecutionConfig(backend="slurm", slurm={"time": "01:00:00"}),
        )


def test_a_task_runs_here_whatever_the_backend(tmp_path, monkeypatch, commands):
    from runtraj.execution import worker

    monkeypatch.delenv("SLURM_JOB_ID", raising=False)

    project = _hourly_project(tmp_path, 4)
    every = sorted(set(project.simulations["release"]), key=runner._scattered)
    ran: list[list[str]] = []
    monkeypatch.setattr(
        worker, "run_releases", lambda project, ids, **kw: ran.append(ids)
    )

    table = runner.run(
        project,
        task=(1, 2),
        execution=ExecutionConfig(backend="slurm", slurm={"time": "01:00:00"}),
    )

    assert commands.ran == []  # no sbatch
    assert ran == [[every[1], every[3]]]
    assert sorted(table["release"]) == sorted([every[1], every[3]])
    assert set(table["state"]) == {"pending"}  # nothing was written


# ---------------------------------------------------------------------------
# The fetch before the releases run
# ---------------------------------------------------------------------------


def _downloading_project(tmp_path, n: int) -> Project:
    """A project like :func:`_hourly_project` whose met downloads HRRR."""
    import datetime as dt

    from runtraj.models.hysplit import HysplitMetConfig
    from runtraj.releases import PointRelease

    config = make_project_config(
        tmp_path,
        mets={"hrrr": HysplitMetConfig(directory=tmp_path / "met", download="hrrr")},
    )
    releases = [
        PointRelease(
            time=dt.datetime(2023, 1, 1, h),
            longitude=-111.85,
            latitude=40.77,
            altitude=5.0,
        )
        for h in range(n)
    ]
    return Project.init(tmp_path / "hourly", config=config, releases=releases)


@pytest.mark.parametrize("value", [True, None, {}])
def test_fetch_is_on_with_the_arrays_resources_unless_given(value):
    assert SlurmConfig.model_validate({"fetch": value}).fetch == FetchConfig()
    assert SlurmConfig().fetch == FetchConfig()


def test_fetch_false_turns_it_off_and_a_bad_fetch_setting_is_an_error():
    assert SlurmConfig.model_validate({"fetch": False}).fetch is False
    with pytest.raises(ValueError, match="partiton"):
        SlurmConfig.model_validate({"fetch": {"partiton": "dtn"}})
    with pytest.raises(ValueError, match="Not a Slurm time limit"):
        SlurmConfig.model_validate({"fetch": {"time": "soon"}})


def test_fetch_is_the_setting_of_the_backend_that_runs():
    """A local run's fetch is on or off; a Slurm run's also holds the fetch job's resources."""
    settings = {"local": {"fetch": False}, "slurm": {"time": "01:00:00"}}
    assert ExecutionConfig.model_validate(settings).fetch is False
    slurm = ExecutionConfig.model_validate({"backend": "slurm", **settings})
    assert slurm.fetch == FetchConfig()
    assert ExecutionConfig().fetch is True


@not_on_windows("Slurm and its job scripts are for Linux")
def test_submit_sends_a_fetch_job_first_and_the_array_waits_for_it(
    pending, commands, tmp_path
):
    project = _downloading_project(tmp_path, 3)
    pending.extend(project.releases["release"])
    execution = ExecutionConfig.model_validate(
        {
            "backend": "slurm",
            "slurm": {
                "time": "01:00:00",
                "account": "lin-np",
                "partition": "lin-np",
                "mem": "8G",
                "setup": ["module load gcc"],
                "exclude": "notch345",
                "fetch": {"partition": "notchpeak-dtn", "time": "12:00:00"},
            },
        }
    )
    commands.answers["sbatch"] = [_out("41\n"), _out("42\n")]

    job_id = runner.submit(project, execution=execution)

    assert job_id == "42"
    [fetch, array] = commands.ran
    assert fetch[:2] == ["sbatch", "--parsable"]
    assert Path(fetch[-1]).name == "fetch.sh"
    assert array == [
        "sbatch",
        "--parsable",
        "--dependency=afterany:41",
        str(Path(fetch[-1]).parent / "job.sh"),
    ]
    folder = Path(fetch[-1]).parent
    text = (folder / "fetch.sh").read_text()
    options = _sbatch(text)
    assert options["partition"] == "notchpeak-dtn"
    assert options["time"] == "12:00:00"
    assert options["account"] == "lin-np"  # the array's, unset in fetch
    assert options["mem"] == "8G"
    assert options["output"] == str(folder / "fetch.log")
    assert (folder / "fetch.id").read_text() == "41\n"
    assert "exclude" not in options  # the array's own sbatch options stay its own
    assert "array" not in options
    assert "module load gcc" in text
    exec_, _python, *command = text.splitlines()[-1].split()
    assert exec_ == "exec"
    assert command[:4] == ["-m", "runtraj", "fetch", str(project.directory)]
    assert command[4:6] == ["--releases", str(folder / "releases.parquet")]
    assert command[6:8] == ["--output", str(project.output.directory)]
    assert "--no-skip" not in command
    subprocess.run(["bash", "-n", str(folder / "fetch.sh")], check=True)


def test_the_array_goes_to_the_cluster_its_fetch_job_went_to(
    pending, commands, tmp_path
):
    project = _downloading_project(tmp_path, 1)
    pending.extend(project.releases["release"])
    execution = ExecutionConfig(
        backend="slurm", slurm={"time": "01:00:00", "clusters": "kingspeak,notchpeak"}
    )
    commands.answers["sbatch"] = [_out("41;kingspeak\n"), _out("42;kingspeak\n")]

    runner.submit(project, execution=execution)

    [fetch, array] = commands.ran
    assert (
        _sbatch(Path(fetch[-1]).read_text(encoding="utf-8"))["clusters"]
        == "kingspeak,notchpeak"
    )
    assert array[2:4] == ["--dependency=afterany:41", "--clusters=kingspeak"]


def test_a_met_without_download_gets_no_fetch_job(pending, commands, tmp_path):
    project = _hourly_project(tmp_path, 2)
    pending.extend(project.releases["release"])
    commands.answers["sbatch"] = [_out("42\n")]

    runner.submit(
        project, execution=ExecutionConfig(backend="slurm", slurm={"time": "01:00:00"})
    )

    [array] = commands.ran
    assert Path(array[-1]).name == "job.sh"
    assert not (Path(array[-1]).parent / "fetch.sh").exists()


def test_fetch_false_sends_no_fetch_job(pending, commands, tmp_path):
    project = _downloading_project(tmp_path, 2)
    pending.extend(project.releases["release"])
    commands.answers["sbatch"] = [_out("42\n")]

    runner.submit(
        project,
        execution=ExecutionConfig(
            backend="slurm", slurm={"time": "01:00:00", "fetch": False}
        ),
    )

    [array] = commands.ran
    assert Path(array[-1]).name == "job.sh"


def test_a_local_run_on_several_cpus_fetches_before_the_workers_start(
    tmp_path, monkeypatch
):
    import pandas as pd

    from runtraj.execution import worker

    project = _downloading_project(tmp_path, 3)
    steps: list[str] = []

    def fetch(project, unrun):
        steps.append(f"fetch {len(unrun)}")
        return pd.DataFrame({"met": [], "file": [], "message": []}, dtype="string")

    monkeypatch.setattr(runner, "fetch_project", fetch)
    monkeypatch.setattr(
        worker, "run_releases", lambda project, ids, **_: steps.append("run")
    )
    monkeypatch.setattr(
        runner, "_status", lambda project, ids: pd.DataFrame({"state": ["pending"]})
    )

    runner.run(project, execution=ExecutionConfig(local={"cpus": 2}))
    assert steps == ["fetch 3", "run"]

    steps.clear()
    runner.run(project, execution=ExecutionConfig(local={"cpus": 1}))
    runner.run(project, execution=ExecutionConfig(local={"cpus": 2, "fetch": False}))
    assert steps == ["run", "run"]


def test_a_local_fetch_that_cannot_reach_the_archive_lets_the_run_go(
    tmp_path, monkeypatch, caplog
):
    import pandas as pd

    from runtraj.execution import worker

    project = _downloading_project(tmp_path, 1)
    ran: list[str] = []

    def fetch(project, unrun):
        raise ConnectionError("archive busy")

    monkeypatch.setattr(runner, "fetch_project", fetch)
    monkeypatch.setattr(
        worker, "run_releases", lambda project, ids, **_: ran.append("run")
    )
    monkeypatch.setattr(
        runner, "_status", lambda project, ids: pd.DataFrame({"state": ["pending"]})
    )

    with caplog.at_level("WARNING", logger="runtraj.execution.runner"):
        runner.run(project, execution=ExecutionConfig(local={"cpus": 2}))

    assert ran == ["run"]
    assert "archive busy" in caplog.text


def test_the_fetch_job_takes_the_arrays_resources_and_none_of_its_other_options(
    pending, commands, tmp_path
):
    """With no ``fetch`` settings, the fetch job runs with the array's resources."""
    project = _downloading_project(tmp_path, 1)
    pending.extend(project.releases["release"])
    execution = ExecutionConfig(
        backend="slurm",
        slurm={
            "account": "lin-np",
            "partition": "lin-np",
            "time": "24:00:00",
            "constraint": "skl",
        },
    )
    commands.answers["sbatch"] = [_out("41\n"), _out("42\n")]

    runner.submit(project, execution=execution)

    [fetch, array] = commands.ran
    options = _sbatch(Path(fetch[-1]).read_text(encoding="utf-8"))
    assert options["account"] == "lin-np"
    assert options["partition"] == "lin-np"
    assert options["time"] == "24:00:00"  # the array's, as job.sh has it
    assert "constraint" not in options  # another sbatch option is the array's alone
    assert _sbatch(Path(array[-1]).read_text(encoding="utf-8"))["time"] == "24:00:00"


def test_a_no_skip_submission_fetches_for_every_run_it_makes_again(
    pending, commands, tmp_path, monkeypatch
):
    import pandas as pd

    project = _downloading_project(tmp_path, 2)
    pending.extend(project.releases["release"])
    # Every particles file exists: only a rerun needs the met.
    real_unrun = Project._unrun

    def unrun(self, sel=None, *, skip_existing=True):
        if skip_existing:
            return pd.DataFrame(columns=["release"])
        return real_unrun(self, sel, skip_existing=False)

    monkeypatch.setattr(Project, "_unrun", unrun)
    commands.answers["sbatch"] = [_out("41\n"), _out("42\n")]

    runner.submit(
        project,
        execution=ExecutionConfig(backend="slurm", slurm={"time": "01:00:00"}),
        skip_existing=False,
    )

    [fetch, array] = commands.ran
    assert Path(fetch[-1]).name == "fetch.sh"
    assert (
        Path(fetch[-1])
        .read_text(encoding="utf-8")
        .splitlines()[-1]
        .endswith(" --no-skip")
    )


def test_a_refused_array_cancels_its_fetch_job(pending, commands, tmp_path):
    project = _downloading_project(tmp_path, 1)
    pending.extend(project.releases["release"])
    execution = ExecutionConfig(
        backend="slurm", slurm={"time": "01:00:00", "clusters": "kingspeak,notchpeak"}
    )
    commands.answers["sbatch"] = [
        _out("41;notchpeak\n"),
        _out(returncode=1, stderr="invalid partition specified"),
    ]

    with pytest.raises(RuntimeError, match="invalid partition"):
        runner.submit(project, execution=execution)

    assert commands.ran[-1] == ["scancel", "-M", "notchpeak", "41"]


def test_a_local_fetch_raises_an_error_a_retry_would_repeat(tmp_path, monkeypatch):
    from runtraj.execution import worker

    project = _downloading_project(tmp_path, 1)
    ran: list[str] = []

    def fetch(project, unrun):
        raise PermissionError(13, "Permission denied", str(tmp_path / "met"))

    monkeypatch.setattr(runner, "fetch_project", fetch)
    monkeypatch.setattr(
        worker, "run_releases", lambda project, ids, **_: ran.append("run")
    )

    with pytest.raises(PermissionError):
        runner.run(project, execution=ExecutionConfig(local={"cpus": 2}))
    assert ran == []


def test_a_local_fetch_lets_the_run_go_after_a_dropped_ftp_connection(
    tmp_path, monkeypatch, caplog
):
    """A dropped FTP connection is an EOFError from ftplib, not an OSError."""
    import ftplib

    import pandas as pd

    from runtraj.execution import worker

    project = _downloading_project(tmp_path, 1)
    ran: list[str] = []

    def fetch(project, unrun):
        ftp = ftplib.FTP()
        ftp.file = SimpleNamespace(readline=lambda size: "")  # a closed connection
        ftp.getline()  # raises EOFError from inside ftplib

    monkeypatch.setattr(runner, "fetch_project", fetch)
    monkeypatch.setattr(
        worker, "run_releases", lambda project, ids, **_: ran.append("run")
    )
    monkeypatch.setattr(
        runner, "_status", lambda project, ids: pd.DataFrame({"state": ["pending"]})
    )

    with caplog.at_level("WARNING", logger="runtraj.execution.runner"):
        runner.run(project, execution=ExecutionConfig(local={"cpus": 2}))

    assert ran == ["run"]
    assert "The fetch stopped" in caplog.text

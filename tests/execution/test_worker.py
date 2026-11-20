"""Tests for the worker-side execution functions in ``runtraj.execution.worker``."""

import datetime as dt
from concurrent.futures import Future

import pandas as pd
import pytest
from typer.testing import CliRunner

from runtraj import Project
from runtraj.cli import app
from runtraj.config import ProjectConfig
from runtraj.exceptions import MeteorologyError, WorkerDied
from runtraj.execution import worker
from runtraj.execution.config import ExecutionConfig
from runtraj.execution.worker import run_release, run_releases
from runtraj.models.hysplit import HysplitConfig, Met, MetConfig
from runtraj.output import Output
from runtraj.releases import PointRelease, Release

from ..fixtures.factories import make_met_config, make_project_config

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def release() -> Release:
    return PointRelease(
        time=dt.datetime(2023, 1, 1, 12),
        longitude=-111.85,
        latitude=40.77,
        altitude=5.0,
    )


@pytest.fixture
def other_release() -> Release:
    return PointRelease(
        time=dt.datetime(2023, 1, 1, 13),
        longitude=-111.86,
        latitude=40.78,
        altitude=10.0,
    )


@pytest.fixture
def met_config(tmp_path) -> MetConfig:
    return make_met_config(tmp_path / "met")


@pytest.fixture
def met(met_config) -> Met:
    return Met("hrrr", met_config)


@pytest.fixture
def params() -> HysplitConfig:
    return HysplitConfig(n_hours=-24, numpar=10)


@pytest.fixture
def output(tmp_path) -> Output:
    return Output(tmp_path / "output")


@pytest.fixture
def compute_root(tmp_path):
    return tmp_path / "compute"


def _particles(release) -> pd.DataFrame:
    data = pd.DataFrame(
        {
            "age": [-60.0],
            "particle": [1.0],
            "lon": [-111.9],
            "lat": [40.7],
            "zagl": [10.0],
            "foot": [1e-5],
        }
    )
    data["time"] = pd.Timestamp(release.time) + pd.to_timedelta(data["age"], unit="min")
    return data


def _model_config(tmp_path, **kwargs) -> ProjectConfig:
    return make_project_config(tmp_path, **kwargs)


def _model(tmp_path, releases, **config_kwargs) -> Project:
    return Project.init(
        tmp_path / "proj",
        config=_model_config(tmp_path, **config_kwargs),
        releases=list(releases),
    )


# ---------------------------------------------------------------------------
# run_particles
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_release: variants that share particles run as one group
# ---------------------------------------------------------------------------


def _run_release(project, release, **kwargs):
    return run_release(
        project, str(release.id), compute_root=project.directory / "scratch", **kwargs
    )


def test_a_release_leaves_no_folders_under_the_compute_root(
    tmp_path, release, monkeypatch
):
    project = _model(
        tmp_path, [release], krand=4, variants={"hrrr": {"realizations": 2}}
    )
    compute_root = project.directory / "scratch"

    class _Model:
        name = "hysplit"

        def run(self, release, params, met, window, workdir=None, timeout=None):
            (workdir / "CONTROL").write_text("")
            raise RuntimeError("stop here")

    monkeypatch.setattr("runtraj.models.get_model", lambda name: _Model())

    assert len(_run_release(project, release)) == 2  # both realizations failed
    assert list(compute_root.iterdir()) == []


@pytest.mark.parametrize("numpar", [200, 100], ids=["other-settings", "same-settings"])
def test_runs_that_share_a_compute_root_never_share_a_workdir(
    tmp_path, release, monkeypatch, numpar
):
    """Two projects run the same release and variant name at once in one compute root."""
    first = _model(tmp_path / "a", [release], numpar=100)
    second = _model(tmp_path / "b", [release], numpar=numpar)
    compute_root = tmp_path / "compute"
    ended: dict[str, str | None] = {}

    class _Model:
        name = "hysplit"
        started = 0

        def run(self, release, params, met, window, workdir=None, timeout=None):
            mine = f"run {_Model.started}"
            _Model.started += 1
            (workdir / "CONTROL").write_text(mine)
            if mine == "run 0":  # the second project starts while the first runs
                run_release(second, str(release.id), compute_root=compute_root)
            control = workdir / "CONTROL"
            ended[mine] = control.read_text() if control.exists() else None
            raise RuntimeError("stop here")

    monkeypatch.setattr("runtraj.models.get_model", lambda name: _Model())
    run_release(first, str(release.id), compute_root=compute_root)

    # Each run finds its own input in its workdir when it ends.
    assert ended == {"run 0": "run 0", "run 1": "run 1"}
    assert list(compute_root.iterdir()) == []


# ---------------------------------------------------------------------------
# run_release: failures are recorded with the simulation
# ---------------------------------------------------------------------------


# A connection or I/O error that a retry may get past is not a failure of the
# simulation: no record is written, so it stays unfinished and `runtraj run`
# exits with 3 (runtraj._transient.is_transient sorts the errors).


def _client_error(status, code):
    """Return what botocore raises for a response with this status and error code."""
    error = type("ClientError", (Exception,), {})("the service answered")
    error.response = {
        "ResponseMetadata": {"HTTPStatusCode": status},
        "Error": {"Code": code},
    }
    return error


def _raised_from(outer, inner):
    """Return *outer* with *inner* as its ``__cause__``, as ``raise outer from inner`` does."""
    outer.__cause__ = inner
    return outer


def _fails_with(error):
    def fail(*args, **kwargs):
        raise error

    return fail


def test_an_earlier_record_that_cannot_be_removed_does_not_stop_the_run(
    tmp_path, release, other_release, monkeypatch
):
    """With the output unreachable the old record stays, and the run goes on to the next release."""
    project = _model(tmp_path, [release, other_release])
    monkeypatch.setattr(worker, "run_particles", _fails_with(ConnectionError("down")))
    monkeypatch.setattr(
        Output, "clear_failure", _fails_with(ConnectionError("down too"))
    )

    for each in (release, other_release):
        problems = _run_release(project, each)
        assert "did not finish" in problems[0]


def test_runtraj_run_still_exits_1_for_a_simulation_that_failed(
    tmp_path, release, monkeypatch
):
    project = _model(tmp_path, [release])
    (tmp_path / "met").mkdir(exist_ok=True)
    monkeypatch.setattr(
        worker, "run_particles", _fails_with(MeteorologyError("No met file."))
    )
    result = CliRunner().invoke(app, ["run", str(project.directory)])
    assert result.exit_code == 1, result.output


# ---------------------------------------------------------------------------
# run_releases: inline
# ---------------------------------------------------------------------------


def _fake_run_release(calls: list[dict], status=None):
    """
    A stand-in for run_release that records its calls.

    *status* maps a release to ``"interrupted"`` (it raises
    KeyboardInterrupt, as a preempted run does) or ``"failed"`` (it reports
    one problem); the others complete.
    """

    def fake(
        project,
        release_id,
        *,
        compute_root,
        execution=None,
        skip_existing=True,
        batched=True,
    ):
        calls.append(
            {
                "release": release_id,
                "execution": execution,
                "skip_existing": skip_existing,
            }
        )
        state = (status or {}).get(release_id)
        if state == "interrupted":
            raise KeyboardInterrupt
        return ["hrrr failed during particles (X): boom"] if state == "failed" else []

    return fake


def test_run_releases_inline_runs_in_order_and_logs_progress(
    tmp_path, release, other_release, monkeypatch, caplog
):
    model = _model(tmp_path, [release, other_release])
    ids = [str(r.id) for r in (release, other_release)]
    calls: list[dict] = []
    monkeypatch.setattr(
        worker, "run_release", _fake_run_release(calls, {ids[0]: "failed"})
    )

    with caplog.at_level("INFO", logger="runtraj.execution.worker"):
        run_releases(
            model,
            ids,
            compute_root=tmp_path / "scratch",
            execution=ExecutionConfig(local={"cpus": 1}),
            skip_existing=True,
        )

    assert [c["release"] for c in calls] == ids
    assert caplog.messages == [
        f"[1/2] {ids[0]}: hrrr failed during particles (X): boom",
        f"[2/2] {ids[1]} complete",
    ]


def test_run_releases_empty_ids_runs_nothing(tmp_path, release, monkeypatch):
    model = _model(tmp_path, [release])
    monkeypatch.setattr(
        worker, "run_release", lambda *a, **k: pytest.fail("must not run")
    )
    run_releases(
        model,
        [],
        compute_root=tmp_path / "scratch",
        execution=ExecutionConfig(local={"cpus": 1}),
    )


def test_run_releases_inline_skips_existing_by_default(tmp_path, release, monkeypatch):
    model = _model(tmp_path, [release])
    calls: list[dict] = []
    monkeypatch.setattr(worker, "run_release", _fake_run_release(calls))

    run_releases(
        model,
        [str(release.id)],
        compute_root=tmp_path / "scratch",
        execution=ExecutionConfig(local={"cpus": 1}),
    )
    run_releases(
        model,
        [str(release.id)],
        compute_root=tmp_path / "scratch",
        execution=ExecutionConfig(local={"cpus": 1}),
        skip_existing=False,
    )

    assert [c["skip_existing"] for c in calls] == [True, False]


def test_run_releases_inline_stops_after_interrupt(
    tmp_path, release, other_release, monkeypatch
):
    model = _model(tmp_path, [release, other_release])
    ids = [str(r.id) for r in (release, other_release)]
    calls: list[dict] = []
    monkeypatch.setattr(
        worker, "run_release", _fake_run_release(calls, {ids[0]: "interrupted"})
    )

    run_releases(
        model,
        ids,
        compute_root=tmp_path / "scratch",
        execution=ExecutionConfig(local={"cpus": 1}),
    )

    assert [c["release"] for c in calls] == [ids[0]]


# ---------------------------------------------------------------------------
# run_releases: process pool (plumbing only, with a synchronous fake executor)
# ---------------------------------------------------------------------------


class _FakeExecutor:
    """
    Synchronous stand-in for ``ProcessPoolExecutor``.

    Runs the initializer in-process at construction and the function at
    ``submit``, returning a finished future that holds its result or its
    error. Records how it was stopped.
    """

    instances: list["_FakeExecutor"] = []

    def __init__(self, max_workers, initializer=None, initargs=()):
        self.max_workers = max_workers
        self.submitted = 0
        self.shutdowns: list[dict] = []
        self._processes: dict = {}
        if initializer is not None:
            initializer(*initargs)
        _FakeExecutor.instances.append(self)

    def submit(self, func, item):
        self.submitted += 1
        future: Future = Future()
        try:
            future.set_result(func(item))
        except Exception as error:  # a KeyboardInterrupt goes up, as a signal does
            future.set_exception(error)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.shutdowns.append({"wait": wait, "cancel_futures": cancel_futures})


@pytest.fixture
def fake_pool(monkeypatch):
    """Patch the executor and keep the worker's module-level pool state isolated."""
    _FakeExecutor.instances = []
    monkeypatch.setattr(worker, "ProcessPoolExecutor", _FakeExecutor)
    monkeypatch.setattr(worker, "_check_workers_can_start", lambda cpus: None)
    # The initializer installs a SIGTERM handler; keep it out of the test process.
    monkeypatch.setattr(worker.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_POOL_PROJECT", None)
    monkeypatch.setattr(worker, "_POOL_COMPUTE_ROOT", None)
    monkeypatch.setattr(worker, "_POOL_SKIP", True)
    return _FakeExecutor


def test_run_releases_pool_rebuilds_the_project_in_each_worker(
    tmp_path, release, other_release, monkeypatch, fake_pool
):
    model = _model(tmp_path, [release, other_release])
    ids = list(model.releases["release"])
    calls: list[dict] = []
    monkeypatch.setattr(worker, "run_release", _fake_run_release(calls))

    run_releases(
        model,
        ids,
        compute_root=tmp_path / "scratch",
        execution=ExecutionConfig(local={"cpus": 2}),
        skip_existing=False,
    )

    [pool] = fake_pool.instances
    assert pool.max_workers == 2
    # A normal finish waits for the workers to exit on their own.
    assert pool.shutdowns == [{"wait": True, "cancel_futures": False}]
    # The initializer opened the project again from its directory.
    assert worker._POOL_PROJECT is not None
    assert worker._POOL_PROJECT is not model
    assert worker._POOL_PROJECT.directory == model.directory
    assert tmp_path / "scratch" == worker._POOL_COMPUTE_ROOT
    assert worker._POOL_SKIP is False
    assert ExecutionConfig(local={"cpus": 2}) == worker._POOL_EXECUTION
    assert sorted(c["release"] for c in calls) == sorted(ids)
    assert [c["skip_existing"] for c in calls] == [False, False]


def test_run_releases_pool_logs_every_release_once(
    tmp_path, release, other_release, monkeypatch, fake_pool, caplog
):
    model = _model(tmp_path, [release, other_release])
    ids = list(model.releases["release"])
    monkeypatch.setattr(
        worker, "run_release", _fake_run_release([], {ids[0]: "failed"})
    )

    with caplog.at_level("INFO", logger="runtraj.execution.worker"):
        run_releases(
            model,
            ids,
            compute_root=tmp_path / "scratch",
            execution=ExecutionConfig(local={"cpus": 2}),
        )

    # Releases are logged as they finish, in either order, counted 1 then 2.
    lines = {m.split("] ", 1)[1] for m in caplog.messages}
    assert lines == {
        f"{ids[0]}: hrrr failed during particles (X): boom",
        f"{ids[1]} complete",
    }
    assert sorted(m.split("]")[0] for m in caplog.messages) == ["[1/2", "[2/2"]


def test_run_releases_pool_stops_its_workers_and_says_how_far_it_got(
    tmp_path, release, other_release, monkeypatch, fake_pool, caplog
):
    model = _model(tmp_path, [release, other_release])
    ids = list(model.releases["release"])
    calls: list[dict] = []
    # The fake runs each release as it is submitted, so the second is stopped
    # before the first one's result is read.
    monkeypatch.setattr(
        worker, "run_release", _fake_run_release(calls, {ids[1]: "interrupted"})
    )

    with caplog.at_level("INFO", logger="runtraj.execution.worker"):
        run_releases(
            model,
            ids,
            compute_root=tmp_path / "scratch",
            execution=ExecutionConfig(local={"cpus": 2}),
        )

    [pool] = fake_pool.instances
    # The workers are stopped, and what has not started is cancelled.
    assert pool.shutdowns == [{"wait": False, "cancel_futures": True}]
    assert [c["release"] for c in calls] == ids
    assert "stopped after 0 of 2 releases; the rest did not run" in caplog.text


def test_a_pool_worker_exits_on_sigterm_and_ignores_ctrl_c(
    tmp_path, release, monkeypatch, fake_pool
):
    """A worker that survived SIGTERM would keep the stop waiting for ever."""
    import signal

    model = _model(tmp_path, [release])
    handlers: dict[int, object] = {}
    monkeypatch.setattr(
        worker.signal,
        "signal",
        lambda signum, handler: handlers.update({signum: handler}),
    )

    worker._init_pool_worker(
        str(model.directory),
        str(model.output.directory),
        str(tmp_path / "scratch"),
        ExecutionConfig(local={"cpus": 2}),
        True,
    )

    assert handlers[signal.SIGINT] is signal.SIG_IGN
    handler = handlers[signal.SIGTERM]
    assert callable(handler)
    with pytest.raises(SystemExit):
        handler(signal.SIGTERM, None)


def test_run_releases_pool_dead_worker_stops_the_run_and_names_what_was_running(
    tmp_path, release, other_release, monkeypatch, fake_pool
):
    """A worker killed outright breaks the pool: stop, and name the releases in progress."""
    from concurrent.futures.process import BrokenProcessPool

    model = _model(tmp_path, [release, other_release])
    ids = list(model.releases["release"])

    def kill(*args, **kwargs):
        raise BrokenProcessPool("a process was terminated abruptly")

    monkeypatch.setattr(worker, "run_release", kill)

    with pytest.raises(WorkerDied) as stopped:
        run_releases(
            model,
            ids,
            compute_root=tmp_path / "scratch",
            execution=ExecutionConfig(local={"cpus": 2}),
        )

    message = str(stopped.value)
    assert all(rid in message for rid in ids)
    assert "memory" in message
    assert "run it again" in message
    [pool] = fake_pool.instances
    assert pool.shutdowns == [{"wait": False, "cancel_futures": True}]


#: A transport model in its own package, which pool workers import by path.
_TOY_PACKAGE = """
import pandas as pd
from pydantic import BaseModel
from runtraj.models import ModelRun, TransportConfig

class ToyConfig(TransportConfig):
    pass

class ToyMet(BaseModel):
    weather: str

    def settings(self):
        return {"weather": self.weather}

class ToyModel:
    name = "toy"
    config_class = ToyConfig
    met_config_class = ToyMet

    def sha256(self, config):
        return None

    def version(self, config):
        return "1.0"

    def data_files(self, config):
        return None

    def run(self, release, config, met, window, workdir=None, timeout=None):
        particles = pd.DataFrame({
            "particle": [1, 2, 1, 2],
            "age": [0, 0, -60, -60],
            "lon": release.longitude,
            "lat": release.latitude,
            "zagl": release.altitude,
            "foot": [0.0, 0.0, 0.01, 0.01],
        })
        return ModelRun(particles=particles)
"""


def test_a_run_on_two_cpus_writes_to_the_output_it_was_given(tmp_path):
    """Pool workers open the project again; they must write where the parent looks."""
    import os
    import subprocess
    import sys

    (tmp_path / "toypool").mkdir()
    (tmp_path / "toypool" / "__init__.py").write_text(_TOY_PACKAGE)
    project = tmp_path / "project"
    project.mkdir()
    (project / "config.yaml").write_text(
        "models:\n  toy:\n    class: toypool.ToyModel\n    n_hours: -1\n"
        "mets:\n  toy:\n    weather: hrrr\n"
        "variants:\n  toy: {}\n"
    )
    (project / "releases.csv").write_text(
        "time,longitude,latitude,altitude\n"
        + "".join(f"2023-01-01 {h:02d}:00:00,-111.85,40.77,5.0\n" for h in range(4))
    )
    override = tmp_path / "elsewhere"

    done = subprocess.run(
        [sys.executable, "-m", "runtraj", "run", str(project), "--cpus", "2"]
        + ["--output", str(override), "--compute-root", str(tmp_path / "work")],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(list(override.glob("particles/*/date=*/*.parquet"))) == 4
    assert not (project / "output").exists()


#: A toy model whose run at altitude 99 is killed outright, as the out-of-memory
#: killer would, when ``TOY_KILL`` is set; the others wait, so they are still
#: in progress when the pool finds its worker dead.
_KILLING_TOY_PACKAGE = """
import os
import signal
import time

from toypool import ToyModel

class KillingToy(ToyModel):
    def run(self, release, config, met, window, workdir=None, timeout=None):
        if os.environ.get("TOY_KILL"):
            if release.altitude == 99.0:
                os.kill(os.getpid(), signal.SIGKILL)  # the worker's own pid
            time.sleep(30)  # until the pool stops it
        return super().run(release, config, met, window, workdir, timeout)
"""


def test_a_worker_killed_outright_stops_the_run_and_a_rerun_finishes_it(tmp_path):
    """A pool worker that dies without Python's say used to hang the run for ever."""
    import os
    import subprocess
    import sys

    for name, source in (("toypool", _TOY_PACKAGE), ("toykill", _KILLING_TOY_PACKAGE)):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text(source)
    project = tmp_path / "project"
    project.mkdir()
    (project / "config.yaml").write_text(
        "models:\n  toy:\n    class: toykill.KillingToy\n    n_hours: -1\n"
        "mets:\n  toy:\n    weather: hrrr\n"
        "variants:\n  toy: {}\n"
    )
    # The first two start together on two cpus; the killed one is the first.
    altitudes = [99.0, 5.0, 5.0, 5.0]
    (project / "releases.csv").write_text(
        "time,longitude,latitude,altitude\n"
        + "".join(
            f"2023-01-01 {h:02d}:00:00,-111.85,40.77,{altitude}\n"
            for h, altitude in enumerate(altitudes)
        )
    )
    command = [sys.executable, "-m", "runtraj", "run", str(project), "--cpus", "2"]
    command += ["--compute-root", str(tmp_path / "work")]
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}

    # On main this hangs; the timeout turns that into a failure.
    killed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
        env={**env, "TOY_KILL": "1"},
    )

    ids = list(Project(project).releases["release"])
    assert killed.returncode == 3, killed.stdout + killed.stderr
    # It names the two releases in progress, not the two that had not started.
    assert all(rid in killed.stderr for rid in ids[:2]), killed.stderr
    assert not any(rid in killed.stderr for rid in ids[2:]), killed.stderr
    assert "memory" in killed.stderr
    assert "run it again" in killed.stderr

    rerun = subprocess.run(
        command, capture_output=True, text=True, timeout=120, env=env
    )

    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert len(list((project / "output").glob("particles/*/date=*/*.parquet"))) == 4


@pytest.mark.parametrize(
    ("alive", "died"),
    [([True, True], False), ([True, False], True), ([False, True], True)],
)
def test_a_dead_pool_worker_is_found_by_looking_at_the_workers(alive, died):
    """The loop looks at the workers itself, since the pool can miss a kill."""
    from types import SimpleNamespace

    processes = {
        i: SimpleNamespace(is_alive=lambda state=state: state)
        for i, state in enumerate(alive)
    }
    executor = SimpleNamespace(_processes=processes)

    assert worker._a_worker_died(executor) is died


# ---------------------------------------------------------------------------
# A batched transport model
# ---------------------------------------------------------------------------


def test_a_pool_worker_gets_its_share_of_the_cpus_as_omp_num_threads(monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "unset below")
    monkeypatch.delenv("OMP_NUM_THREADS")
    monkeypatch.setattr(worker, "_available_cpus", lambda: 16)
    worker._share_threads(4)
    assert worker.os.environ["OMP_NUM_THREADS"] == "4"


def test_a_pool_worker_gets_one_thread_when_workers_outnumber_the_cpus(monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "unset below")
    monkeypatch.delenv("OMP_NUM_THREADS")
    monkeypatch.setattr(worker, "_available_cpus", lambda: 2)
    worker._share_threads(8)
    assert worker.os.environ["OMP_NUM_THREADS"] == "1"


def test_a_pool_worker_keeps_the_omp_num_threads_the_run_set(monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "3")
    monkeypatch.setattr(worker, "_available_cpus", lambda: 16)
    worker._share_threads(4)
    assert worker.os.environ["OMP_NUM_THREADS"] == "3"


def test_the_pool_initializer_shares_the_cpus(
    tmp_path, release, monkeypatch, fake_pool
):
    model = _model(tmp_path, [release])
    monkeypatch.setenv("OMP_NUM_THREADS", "unset below")
    monkeypatch.delenv("OMP_NUM_THREADS")
    monkeypatch.setattr(worker, "_available_cpus", lambda: 8)
    worker._init_pool_worker(
        str(model.directory),
        str(model.output.directory),
        str(tmp_path / "scratch"),
        ExecutionConfig(local={"cpus": 2}),
        True,
    )
    assert worker.os.environ["OMP_NUM_THREADS"] == "4"

"""Shared fixtures for the runtraj test suite."""

import datetime as dt
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from runtraj.config import ProjectConfig
from runtraj.releases import ColumnRelease, MultiPointRelease, PointRelease
from tests.fixtures.factories import blocks

from .fixtures import replay
from .fixtures.factories import make_hysplit_dir, make_met_config
from .fixtures.runs import FakeRuns

# ---------------------------------------------------------------------------
# Marker - apply to every test that needs real met + HYSPLIT
# ---------------------------------------------------------------------------

integration = pytest.mark.integration

# Parametrized fixtures that run HYSPLIT once per parameter for several
# tests. CI runs the integration tests on several workers
# (pytest-xdist, ``--dist loadgroup``); grouping keeps the tests of one parameter
# on one worker, so each run still happens once.
_SHARED_RUNS = ("scenario_outputs", "hysplit_run")


# tryfirst: xdist reads the groups in its own pytest_collection_modifyitems.
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    """Put the tests that share one run of a ``_SHARED_RUNS`` fixture in one xdist group."""
    for item in items:
        callspec = getattr(item, "callspec", None)
        if callspec is None:
            continue
        for name in _SHARED_RUNS:
            if name in callspec.params:
                value = callspec.params[name]
                group = f"{name}-{getattr(value, 'name', value)}"
                item.add_marker(pytest.mark.xdist_group(group))


# ---------------------------------------------------------------------------
# HYSPLIT's hycs_std
# ---------------------------------------------------------------------------

#: The markers of the tests that run a real ``hycs_std``.
_REAL_HYSPLIT = ("integration",)


def pytest_addoption(parser):
    """Add the options that replay or record HYSPLIT's runs (``tests/fixtures/replay.py``)."""
    group = parser.getgroup("hysplit", "recorded HYSPLIT runs")
    group.addoption(
        "--hysplit-replay",
        metavar="VERSION",
        help="Replay the runs recorded on HYSPLIT VERSION (tests/recorded/hysplit/VERSION) "
        "instead of running a hycs_std from PATH.",
    )
    group.addoption(
        "--hysplit-record",
        metavar="BUILD",
        type=Path,
        help="Run the HYSPLIT folder BUILD (its exec/ and bdyfiles/) and record "
        "each run in --hysplit-cases.",
    )
    group.addoption(
        "--hysplit-cases",
        metavar="DIR",
        type=Path,
        help="Where --hysplit-record saves the runs.",
    )


def pytest_configure(config):
    """
    Put a replaying or recording HYSPLIT folder first on ``PATH``, for the whole session.

    It goes on ``PATH`` before the tests are collected, because some modules
    skip themselves when there is no ``hyts_std``. Each xdist worker makes its
    own folder; a replay's misses go to one folder, which the controller
    clears and reports (:func:`pytest_terminal_summary`).
    """
    version = config.getoption("--hysplit-replay")
    build = config.getoption("--hysplit-record")
    if version is None and build is None:
        return
    if version is not None and build is not None:
        raise pytest.UsageError("Give --hysplit-replay or --hysplit-record, not both.")
    if build is not None:
        cases = config.getoption("--hysplit-cases")
        if cases is None:
            raise pytest.UsageError("--hysplit-record needs --hysplit-cases.")
        misses = None
    else:
        cases = replay.RECORDED / version
        if not cases.is_dir():
            raise pytest.UsageError(
                f"No runs are recorded on HYSPLIT {version}: {cases}"
            )
        misses = replay.RECORDED.parent / ".misses" / version
        if not hasattr(config, "workerinput"):  # the controller, or no xdist
            shutil.rmtree(misses, ignore_errors=True)
        config._hysplit_misses = misses
    folder = Path(tempfile.mkdtemp(prefix="runtraj-hysplit-"))
    config._hysplit_folder = folder
    exec_dir = replay.make_folder(folder, cases, build, misses)
    os.environ["PATH"] = f"{exec_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def pytest_unconfigure(config):
    """Remove the session's replaying or recording HYSPLIT folder."""
    folder = getattr(config, "_hysplit_folder", None)
    if folder is not None:
        shutil.rmtree(folder, ignore_errors=True)


def pytest_terminal_summary(terminalreporter, config):
    """List the runs a replay had no recording of, and say how to record them."""
    folder = getattr(config, "_hysplit_misses", None)
    if folder is None or hasattr(config, "workerinput"):
        return
    found = replay.misses(folder)
    if found:
        terminalreporter.section("HYSPLIT runs with no recording")
        for path in found:
            terminalreporter.write_line(str(path))
        terminalreporter.write_line(
            f"{len(found)} run(s) had inputs no recorded case has; they are kept above. "
            "If runtraj writes them on purpose, record again: just record "
            "(CONTRIBUTING.md, 'Testing against real HYSPLIT')."
        )


#: What the stand-in prints when it starts, as ``hycs_std`` does.
STAND_IN_VERSION = "hysplit.v5.1.0-test"


@pytest.fixture(scope="session")
def stand_in_hycs_std(tmp_path_factory) -> Path:
    """
    The ``exec/`` folder of a stand-in HYSPLIT, whose ``hycs_std`` prints a version and stops.

    It stops as ``hycs_std`` does without ``CONTROL``. runtraj does not
    include HYSPLIT, and a run's settings hold the SHA-256 of the
    ``hycs_std`` it runs and of its data tables, so the unit tests use these
    instead. The tables are in ``bdyfiles/`` beside ``exec/``.
    """
    folder = make_hysplit_dir(
        tmp_path_factory.mktemp("hysplit"),
        STAND_IN_VERSION,
        body="echo ' Last Changed Date: 2021-05-13'\nexit 2\n",
    )
    return folder / "exec"


@pytest.fixture(autouse=True)
def _hycs_std_on_path(request, monkeypatch):
    """
    Put the stand-in ``hycs_std`` first on ``PATH`` for a unit test; skip a real-HYSPLIT test without one.

    A test marked ``integration`` or ``fidelity`` runs the first ``hycs_std``
    on ``PATH``, and is
    skipped when there is none.
    """
    if any(request.node.get_closest_marker(m) for m in _REAL_HYSPLIT):
        if shutil.which("hycs_std") is None:
            pytest.skip(
                "No hycs_std on PATH. Put the exec/ folder of a HYSPLIT build on PATH."
            )
        return
    stand_in = request.getfixturevalue("stand_in_hycs_std")
    monkeypatch.setenv("PATH", f"{stand_in}{os.pathsep}{os.environ.get('PATH', '')}")


# ---------------------------------------------------------------------------
# Met directory
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _met_catalogs_stay_out_of_the_home_folder(tmp_path_factory, monkeypatch):
    """A met without a ``subgrid_dir`` keeps its catalog in the user's cache: not a real one."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.getbasetemp() / "cache"))


_TESTS_DIR = Path(__file__).parent
_MET_CACHE = _TESTS_DIR / "met_cache"

# SLV bounding box for bbox-cropped HRRRArchive downloads. Each 6h block is a
# 3.42 GB full-domain file, cropped to 130 x 129 cells (30.2 MB) once it is down.
_SLV_BBOX = (-114.0, 39.0, -110.0, 42.0)  # (west, south, east, north)

# Individual 6h HRRR blocks needed by all 12 fidelity scenarios.
# Each entry is a (block_start, block_end) pair that maps to exactly one file.
# Winter blocks cover day_backward (24h), which needs blocks from the previous day.
# Bump the GHA cache key in tests.yml and docs.yml when you add blocks here.
_MET_BLOCKS: list[tuple[str, str]] = [
    # Winter 2021-01-15 (includes day_backward 24h)
    ("2021-01-14 06:00", "2021-01-14 11:59"),
    ("2021-01-14 12:00", "2021-01-14 17:59"),
    ("2021-01-14 18:00", "2021-01-14 23:59"),
    ("2021-01-15 00:00", "2021-01-15 05:59"),
    ("2021-01-15 06:00", "2021-01-15 11:59"),
    # Summer 2021-07-15
    ("2021-07-15 00:00", "2021-07-15 05:59"),
    ("2021-07-15 06:00", "2021-07-15 11:59"),
]


@pytest.fixture(scope="session")
def met_dir(request) -> Path:
    """
    Path to HRRR ARL met files for the integration tests (:func:`_find_met_dir`).

    A replay (``--hysplit-replay``) checks first that they are the files the
    runs were recorded on (:func:`_check_recorded_met`).
    """
    path = _find_met_dir()
    version = request.config.getoption("--hysplit-replay")
    if version is not None:
        _check_recorded_met(replay.RECORDED / version, path)
    return path


def _check_recorded_met(cases: Path, met: Path) -> None:
    """
    Fail unless *met* holds the met files the cases at *cases* were recorded on, byte for byte.

    A replay gives back HYSPLIT's output on the recorded files, while runtraj
    reads the files here: other files (another crop, another arlmet) make
    tests that compare the two fail for no reason of runtraj's.
    """
    import hashlib

    import yaml

    recorded = yaml.safe_load((cases / "recorded.yaml").read_text())["met_files"]
    differ = []
    for name, sha in recorded.items():
        path = met / name
        if not path.is_file():
            differ.append(f"{name}: missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            differ.append(f"{name}: other contents")
    if differ:
        pytest.fail(
            f"The met files in {met} differ from the ones the runs in {cases} were "
            "recorded on (recorded.yaml):\n  " + "\n  ".join(differ) + "\n"
            "Record again on these files (just record), or use the recorded ones.",
            pytrace=False,
        )


def _find_met_dir() -> Path:
    """
    Path to HRRR ARL met files for fidelity tests.

    Resolution order:
    1. RUNTRAJ_TEST_MET_DIR env var — point at a pre-downloaded cache directory.
    2. RUNTRAJ_TEST_FETCH_MET=1 — download all 7 bbox-cropped 6h blocks in parallel
       via HRRRArchive into tests/met_cache/ (gitignored; cached in CI).
       Already-present files are skipped automatically. Each block is
       downloaded whole (3.42 GB) and then cropped, so the 7 blocks download
       23.9 GB, all staged in tests/met_cache/ at once, and keep 211 MB.
    3. Otherwise skip.
    """
    env = os.environ.get("RUNTRAJ_TEST_MET_DIR")
    if env and Path(env).exists():
        # Absolute, so a program that runs in its own working directory
        # finds the files too when the variable is a relative path.
        return Path(env).resolve()

    if os.environ.get("RUNTRAJ_TEST_FETCH_MET") == "1":
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from arlmet.archives import HRRRArchive

        _MET_CACHE.mkdir(exist_ok=True)
        archive = HRRRArchive()

        with ThreadPoolExecutor(max_workers=len(_MET_BLOCKS)) as pool:
            futures = {
                pool.submit(archive.fetch, s, e, dest_dir=_MET_CACHE, bbox=_SLV_BBOX): (
                    s,
                    e,
                )
                for s, e in _MET_BLOCKS
            }
            for fut in as_completed(futures):
                fut.result()  # surface any download errors immediately

        return _MET_CACHE

    pytest.skip(
        "Met files not available. Set RUNTRAJ_TEST_MET_DIR to a directory with "
        "HRRR ARL files, or set RUNTRAJ_TEST_FETCH_MET=1 to download automatically."
    )


# ---------------------------------------------------------------------------
# Release fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def point_release():
    """A simple single-point release."""
    return PointRelease(
        time=dt.datetime(2023, 1, 1, 12, 0),
        longitude=-111.85,
        latitude=40.77,
        altitude=5.0,
    )


@pytest.fixture
def column_release():
    """A vertical-column release (two heights, same lon/lat)."""
    return ColumnRelease(
        time=dt.datetime(2023, 1, 1, 12, 0),
        longitude=-111.85,
        latitude=40.77,
        bottom=5.0,
        top=50.0,
    )


@pytest.fixture
def multipoint_release():
    """A multi-point release (three different locations)."""
    return MultiPointRelease(
        time=dt.datetime(2023, 1, 1, 12, 0),
        longitudes=[-111.85, -111.86, -111.84],
        latitudes=[40.77, 40.78, 40.76],
        altitudes=[5.0, 5.0, 5.0],
    )


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def met_config(tmp_path):
    """HysplitMetConfig pointing at a temporary directory."""
    return make_met_config(tmp_path / "met")


@pytest.fixture
def model_config(tmp_path, met_config):
    """Minimal ProjectConfig with one met entry."""
    return ProjectConfig(
        **blocks(
            n_hours=-24, numpar=100, mets={"hrrr": met_config}, variants={"hrrr": {}}
        )
    )


@pytest.fixture
def transport_runs(monkeypatch) -> FakeRuns:
    """Replace the transport model run with :class:`~tests.fixtures.runs.FakeRuns`, and return it."""
    runs = FakeRuns()
    monkeypatch.setattr("runtraj.execution.worker.run_model", runs)
    return runs

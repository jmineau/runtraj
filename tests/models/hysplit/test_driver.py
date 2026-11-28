"""Tests for HYSPLIT's input files, running hycs_std, and reading its particles."""

import datetime as dt
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from runtraj import _process
from runtraj.exceptions import HYSPLITNotFoundError, SimulationError
from runtraj.models import run_model
from runtraj.models.hysplit import (
    FailureReason,
    HysplitConfig,
    HysplitModel,
    driver,
    model,
    read_particle_dat,
    write_inputs,
)
from runtraj.models.hysplit.config import FIXED_SETUP, HYSPLIT_VARSIWANT
from runtraj.models.hysplit.control import ControlFile
from runtraj.models.hysplit.driver import (
    MET_STOP_CELLS,
    PARTICLE_FILE,
    cells_from_edge,
    check_met_stop,
    setup_entries,
    setup_seed,
    version_at_least,
    winderrtf,
    zicontroltf,
    ziscale_factors,
)
from runtraj.models.run import _release_points
from tests.fixtures.platform import not_on_windows

from ...fixtures.factories import (
    STAND_IN_TABLES,
    make_hysplit_dir,
    make_met_config,
    make_met_files,
    program_path,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_control(
    release, n_hours=-24, met_files=None, emisshrs=0.01, w_option=0, z_top=25000.0
):
    if met_files is None:
        met_files = [Path("/met/hrrr_2023010100")]
    return ControlFile(
        release_time=release.time,
        locations=release.coords(),
        emisshrs=emisshrs,
        n_hours=n_hours,
        w_option=w_option,
        z_top=z_top,
        met_files=met_files,
    )


# ---------------------------------------------------------------------------
# ControlFile.write / .read roundtrip - single-point release
# ---------------------------------------------------------------------------


def test_control_file_roundtrip_single_point(point_release, tmp_path):
    cf = _make_control(point_release)
    path = tmp_path / "CONTROL"
    cf.write(path)
    loaded = ControlFile.read(path)

    assert loaded.release_time == point_release.time
    assert loaded.locations == point_release.coords()
    assert loaded.n_hours == -24
    assert loaded.w_option == 0
    assert loaded.z_top == 25000.0
    assert len(loaded.met_files) == 1


@pytest.mark.parametrize("year", [1940, 1968, 1969, 1999, 2000, 2039])
def test_control_file_year_roundtrip(year, tmp_path):
    """A two-digit year reads as HYSPLIT does: 40 to 99 is 19YY, 00 to 39 is 20YY."""
    control = ControlFile(
        release_time=dt.datetime(year, 1, 1),
        locations=[(-112.0, 40.5, 10.0)],
        emisshrs=0.01,
        n_hours=-24,
        w_option=0,
        z_top=25000.0,
        met_files=[Path("/met/file")],
    )
    path = tmp_path / "CONTROL"
    control.write(path)
    assert ControlFile.read(path).release_time.year == year


@pytest.mark.parametrize("year", [1939, 2040])
def test_control_file_refuses_year_outside_two_digit_range(year, tmp_path):
    """HYSPLIT would read the year a century off, so CONTROL is not written."""
    control = ControlFile(
        release_time=dt.datetime(year, 1, 1),
        locations=[(-112.0, 40.5, 10.0)],
        emisshrs=0.01,
        n_hours=-24,
        w_option=0,
        z_top=25000.0,
        met_files=[Path("/met/file")],
    )
    with pytest.raises(ValueError, match="1940 to 2039"):
        control.write(tmp_path / "CONTROL")


def test_control_file_roundtrip_emisshrs(point_release, tmp_path):
    cf = _make_control(point_release, emisshrs=0.5)
    path = tmp_path / "CONTROL"
    cf.write(path)
    loaded = ControlFile.read(path)
    assert loaded.emisshrs == pytest.approx(0.5)


def test_control_file_roundtrip_fractional_emisshrs(point_release, tmp_path):
    cf = _make_control(point_release, emisshrs=0.01)
    path = tmp_path / "CONTROL"
    cf.write(path)
    loaded = ControlFile.read(path)
    assert loaded.emisshrs == pytest.approx(0.01)


def test_control_file_roundtrip_n_hours_forward(point_release, tmp_path):
    cf = _make_control(point_release, n_hours=24)
    path = tmp_path / "CONTROL"
    cf.write(path)
    loaded = ControlFile.read(path)
    assert loaded.n_hours == 24


def test_control_file_roundtrip_release_time(point_release, tmp_path):
    cf = _make_control(point_release)
    path = tmp_path / "CONTROL"
    cf.write(path)
    loaded = ControlFile.read(path)
    assert loaded.release_time == point_release.time


def test_control_file_lists_a_location_as_latitude_longitude_height(tmp_path):
    """CONTROL's order; ``locations`` is (lon, lat, height), like a release's points."""
    path = tmp_path / "CONTROL"
    ControlFile(
        release_time=dt.datetime(2023, 7, 15, 18, 5),
        locations=[(-111.848, 40.766, 10.0), (-112.0, 40.5, 250.0)],
        emisshrs=0.01,
        n_hours=-24,
        w_option=0,
        z_top=25000.0,
        met_files=[Path("/met/hrrr")],
    ).write(path)

    assert path.read_text().splitlines()[:4] == [
        "23 07 15 18 05",
        "2",
        "40.766 -111.848 10.0",
        "40.5 -112.0 250.0",
    ]
    assert ControlFile.read(path).locations == [
        (-111.848, 40.766, 10.0),
        (-112.0, 40.5, 250.0),
    ]


def test_control_file_roundtrip_multiple_met_files(point_release, tmp_path):
    met = [
        Path("/met/hrrr_2022123100"),
        Path("/met/hrrr_2023010100"),
    ]
    cf = _make_control(point_release, met_files=met)
    path = tmp_path / "CONTROL"
    cf.write(path)
    loaded = ControlFile.read(path)
    assert len(loaded.met_files) == 2
    assert loaded.met_files[0].name == "hrrr_2022123100"
    assert loaded.met_files[1].name == "hrrr_2023010100"


def test_control_lists_the_met_files_as_one_grid(point_release, tmp_path):
    """HYSPLIT reads at most 12 grids but 128 files of one grid."""
    met = [Path(f"/met/hrrr_{i:02d}") for i in range(20)]
    path = tmp_path / "CONTROL"
    _make_control(point_release, met_files=met).write(path)

    lines = path.read_text().splitlines()
    count = lines.index("/met/") - 1
    assert lines[count] == "1 20"
    assert [f.name for f in ControlFile.read(path).met_files] == [m.name for m in met]


def test_control_reads_a_met_count_of_one_file_per_grid(point_release, tmp_path):
    """A CONTROL file may give a bare count."""
    met = [Path("/met/hrrr_a"), Path("/met/hrrr_b")]
    path = tmp_path / "CONTROL"
    _make_control(point_release, met_files=met).write(path)
    path.write_text(path.read_text().replace("\n1 2\n", "\n2\n"))

    assert [f.name for f in ControlFile.read(path).met_files] == ["hrrr_a", "hrrr_b"]


def test_control_file_roundtrip_column_release(column_release, tmp_path):
    cf = _make_control(column_release)
    path = tmp_path / "CONTROL"
    cf.write(path)
    loaded = ControlFile.read(path)
    assert loaded.locations == column_release.coords()
    assert [height for _, _, height in loaded.locations] == [
        column_release.bottom,
        column_release.top,
    ]


def test_control_file_roundtrip_multipoint_release(multipoint_release, tmp_path):
    cf = _make_control(multipoint_release)
    path = tmp_path / "CONTROL"
    cf.write(path)
    loaded = ControlFile.read(path)
    assert loaded.locations == multipoint_release.coords()


def test_control_file_read_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ControlFile.read(tmp_path / "CONTROL")


# ---------------------------------------------------------------------------
# Inputs, runs, and particle files
# ---------------------------------------------------------------------------

VARS = ["time", "indx", "long", "lati", "zagl", "foot"]


def _upper_dswf(name):
    """The name as SETUP.CFG gets it: hycs_std 5.2+ fills dswf only as DSWF."""
    return "DSWF" if name == "dswf" else name


def _config(**overrides) -> HysplitConfig:
    """A small config whose particle rows are the six columns of VARS."""
    return HysplitConfig(
        **{"n_hours": -24, "numpar": 10, "varsiwant": VARS} | overrides
    )


def _fake_build(path: Path, marker: str = "fake binary") -> Path:
    """Return a HYSPLIT folder holding a stand-in, executable ``exec/hycs_std``, and stand-in tables."""
    make_hysplit_dir(path)
    program_path(path / "exec").write_text(marker)
    return path


def _inputs(tmp_path, release, config=None) -> Path:
    """Write the input files for *release*, as the model does, and return the working directory."""
    workdir = tmp_path / "run"
    model._write_inputs(
        workdir, release, config or _config(), [tmp_path / "met" / "dummy"]
    )
    return workdir


def _write_particle_dat(path: Path, rows: list[list[float]]) -> None:
    """Write a minimal particle file with one dummy header line."""
    lines = ["header"]
    lines.extend(" ".join(str(v) for v in row) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# read_particle_dat
# ---------------------------------------------------------------------------


def test_read_particle_dat_parses_the_columns(tmp_path):
    dat = tmp_path / PARTICLE_FILE
    _write_particle_dat(
        dat,
        rows=[
            [-60, 1, -111.9, 40.7, 10.0, 1e-5],
            [-120, 1, -112.0, 40.6, 20.0, 2e-5],
        ],
    )

    df = read_particle_dat(dat, VARS)
    assert len(df) == 2
    # HYSPLIT's codes indx, long, lati are the table's particle, lon, lat.
    assert list(df.columns) == ["age", "particle", "lon", "lat", "zagl", "foot"]
    assert df["particle"].iloc[0] == 1
    assert dat.exists()  # the worker removes the working directory, or keeps it


def test_read_particle_dat_rejects_other_columns(tmp_path):
    dat = tmp_path / PARTICLE_FILE
    _write_particle_dat(dat, rows=[[-60, 1, -111.9, 40.7, 10.0, 1e-5]])

    with pytest.raises(ValueError, match="6 columns, expected 2"):
        read_particle_dat(dat, ["time", "indx"])


def test_read_particle_dat_reads_the_values_numpy_reads(tmp_path):
    # HYSPLIT's own layout: the release location on the first line, then
    # right-aligned columns of varying width and digits ("-1.", "1471.0836",
    # "0.0004907309"). Every value must be the float64 numpy.loadtxt reads.
    rng = np.random.default_rng(59)
    formats = [
        "{:10.0f}.",
        "{:9.0f}.",
        "{:11.4f}",
        "{:10.4f}",
        "{:10.4f}",
        "{:10.3f}",
        "{:14.4f}",
        "{:10.4f}",
        "{:10.2f}",
        "{:13.10f}",
    ]
    lines = ["   40.5000000      -112.000000       5.00000000    "]
    for _ in range(2000):
        values = rng.uniform(-200, 2000, len(formats)) * rng.choice([1, 1e-4])
        lines.append(
            " ".join(f.format(v) for f, v in zip(formats, values, strict=True))
        )
    dat = tmp_path / PARTICLE_FILE
    dat.write_text("\n".join(lines) + "\n")

    names = [
        "time",
        "indx",
        "long",
        "lati",
        "zagl",
        "sigw",
        "zsfc",
        "temp",
        "pres",
        "foot",
    ]
    df = read_particle_dat(dat, names)

    expected = np.loadtxt(dat, skiprows=1)
    expected[:, 0] *= 60.0  # HYSPLIT's minutes, read as seconds
    assert df.dtypes.eq(np.float64).all()
    np.testing.assert_array_equal(df.to_numpy(), expected, strict=True)


def test_read_particle_dat_reads_hysplits_minutes_as_seconds(tmp_path):
    dat = tmp_path / PARTICLE_FILE
    _write_particle_dat(
        dat,
        rows=[
            [-1, 1, -111.9, 40.7, 10.0, 0.5],
            [-90, 1, -112.0, 40.6, 20.0, 2.0],
        ],
    )

    df = read_particle_dat(dat, ["time", "indx", "long", "lati", "zagl", "samt"])
    assert df["age"].tolist() == [-60.0, -5400.0]
    assert df["samt"].tolist() == [30.0, 120.0]


def test_read_particle_dat_of_an_empty_file_is_an_empty_table(tmp_path):
    dat = tmp_path / PARTICLE_FILE
    _write_particle_dat(dat, rows=[])

    df = read_particle_dat(dat, VARS)
    assert df.empty
    assert list(df.columns) == ["age", "particle", "lon", "lat", "zagl", "foot"]


# ---------------------------------------------------------------------------
# Running hycs_std
# ---------------------------------------------------------------------------


def _script(workdir: Path, text: str) -> None:
    """Put a shell script where hycs_std is run from."""
    if sys.platform == "win32":
        pytest.skip("the stand-in HYSPLIT is a shell script")
    workdir.mkdir(parents=True, exist_ok=True)
    exe = workdir / "hycs_std"
    exe.write_text("#!/usr/bin/env bash\n" + text)
    exe.chmod(0o755)


def test_run_keeps_fortran_runtime_output_on_failure(tmp_path):
    _script(
        tmp_path,
        "echo 'Fortran runtime error: File already opened in another unit'\nexit 2\n",
    )

    with pytest.raises(SimulationError) as caught:
        driver.run_hycs_std(tmp_path, timeout=5)
    assert caught.value.reason == FailureReason.FORTRAN_RUNTIME_ERROR
    assert "Fortran runtime error" in (tmp_path / "hysplit.log").read_text()
    # The message is HYSPLIT's own line, so a record says what went wrong.
    assert str(caught.value) == (
        "HYSPLIT: Fortran runtime error: File already opened in another unit"
    )


def test_a_varsiwant_name_hycs_std_stops_on_fails_with_its_line(tmp_path):
    """The log's last line says why no particles were written, not only that none were."""
    _script(
        tmp_path,
        "echo 'Weird variable in VARSIWANT(SETUP.CFG):  zfx1'\n"
        f"echo 'header' > {PARTICLE_FILE}\n",
    )

    with pytest.raises(SimulationError) as caught:
        driver.run_hycs_std(tmp_path, timeout=5)
    assert caught.value.reason == FailureReason.NO_PARTICLE_DATA
    assert str(caught.value) == (
        "HYSPLIT: Weird variable in VARSIWANT(SETUP.CFG):  zfx1"
    )


def test_met_that_ran_out_fails_from_hysplits_warning_file(tmp_path):
    """HYSPLIT says so in WARNING, not in its log."""
    _script(
        tmp_path,
        "echo ' WARNING emspnt: exceeding puff limit' > WARNING\n"
        "echo ' WARNING metpos: no more meteorology -     63723240' >> WARNING\n",
    )

    with pytest.raises(SimulationError) as caught:
        driver.run_hycs_std(tmp_path, timeout=5)
    assert caught.value.reason == FailureReason.MET_COVERAGE
    assert str(caught.value) == (
        "HYSPLIT: WARNING metpos: no more meteorology -     63723240"
    )


def test_particles_that_left_the_met_domain_are_not_a_failure(tmp_path):
    _script(
        tmp_path,
        "echo ' WARNING emspnt: exceeding puff limit' > WARNING\n"
        "echo ' WARNING metpos: off spatial domain of all grids' >> WARNING\n",
    )

    driver.run_hycs_std(tmp_path, timeout=5)  # does not raise


def test_run_drops_the_hysf_variables_and_keeps_the_rest(tmp_path, monkeypatch):
    """HYSPLIT 5.4 moves its files to where ``HYSF_*`` names; every other variable stays."""
    monkeypatch.setenv("HYSF_PARTICLE_STILT", str(tmp_path / "elsewhere.dat"))
    monkeypatch.setenv("HYSF_CONTROL", "elsewhere")
    monkeypatch.setenv("RUNTRAJ_TEST_KEPT", "yes")
    _script(tmp_path, "env > seen_env\n")

    driver.run_hycs_std(tmp_path, timeout=5)

    seen = (tmp_path / "seen_env").read_text().splitlines()
    assert "RUNTRAJ_TEST_KEPT=yes" in seen
    assert not [line for line in seen if line.startswith("HYSF_")]


def test_run_times_out_and_keeps_log_output(tmp_path):
    _script(tmp_path, "echo 'starting hycs_std'\nsleep 30\n")

    with pytest.raises(SimulationError, match="timeout") as caught:
        driver.run_hycs_std(tmp_path, timeout=1)
    assert caught.value.reason == FailureReason.TIMEOUT
    assert "starting hycs_std" in (tmp_path / "hysplit.log").read_text()


def _kill_when_started(workdir: Path, sig: int):
    """
    Send *sig* to the script in *workdir* once it has written its pid, from another thread.

    The script must write ``pid`` last, just before ``exec``, so the kill cannot land
    before the script has written what the test reads afterwards.
    """
    import os
    import threading
    import time

    def kill() -> None:
        pid_file = workdir / "pid"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if pid_file.exists() and pid_file.read_text().strip():
                break
            time.sleep(0.02)
        os.kill(int(pid_file.read_text()), sig)

    thread = threading.Thread(target=kill, daemon=True)
    thread.start()
    return thread


@not_on_windows("the test needs POSIX permissions, process groups or signals")
def test_run_fails_when_hycs_std_is_killed_from_outside(tmp_path):
    """A kill the run did not send, such as the out-of-memory killer's, is a failure."""
    import signal

    _script(
        tmp_path,
        "echo 'starting hycs_std'\n"
        "printf 'first line\\nlast line of MESSAGE\\n' > MESSAGE\n"
        "echo $$ > pid\n"
        "exec sleep 60\n",
    )
    thread = _kill_when_started(tmp_path, signal.SIGKILL)

    with pytest.raises(SimulationError) as caught:
        driver.run_hycs_std(tmp_path, timeout=30)
    thread.join()

    assert caught.value.reason == FailureReason.EXIT_STATUS
    assert str(caught.value) == "HYSPLIT was killed by SIGKILL before it finished."
    log = (tmp_path / "hysplit.log").read_text()
    assert "starting hycs_std" in log
    assert log.endswith("The end of MESSAGE:\nfirst line\nlast line of MESSAGE\n")


def test_run_fails_when_hycs_std_exits_with_a_status(tmp_path):
    _script(tmp_path, "echo 'starting hycs_std'\nexit 1\n")

    with pytest.raises(SimulationError) as caught:
        driver.run_hycs_std(tmp_path, timeout=5)

    assert caught.value.reason == FailureReason.EXIT_STATUS
    assert str(caught.value) == "HYSPLIT exited with status 1 before it finished."
    # No MESSAGE file, so nothing is added to the log.
    assert (tmp_path / "hysplit.log").read_text() == "starting hycs_std\n"


def test_the_end_of_message_is_the_last_twenty_lines(tmp_path):
    _script(tmp_path, "seq 1 50 > MESSAGE\nexit 2\n")

    with pytest.raises(SimulationError):
        driver.run_hycs_std(tmp_path, timeout=5)

    log = (tmp_path / "hysplit.log").read_text()
    assert log.splitlines()[-21:] == [
        "runtraj: HYSPLIT exited with status 2. The end of MESSAGE:"
    ] + [str(n) for n in range(31, 51)]


def test_a_signal_without_a_name_is_given_by_its_number(tmp_path):
    error = driver._exit_status_error(tmp_path, -200)

    assert str(error) == "HYSPLIT was killed by signal 200 before it finished."
    assert error.reason == FailureReason.EXIT_STATUS


def test_a_known_message_names_the_failure_before_the_exit_status(tmp_path):
    """The met running out stays ``MET_COVERAGE`` when HYSPLIT also exits with a status."""
    _script(
        tmp_path,
        "echo ' WARNING metpos: no more meteorology -     63723240' > WARNING\nexit 1\n",
    )

    with pytest.raises(SimulationError) as caught:
        driver.run_hycs_std(tmp_path, timeout=5)

    assert caught.value.reason == FailureReason.MET_COVERAGE


def test_run_that_exits_with_status_zero_is_not_a_failure(tmp_path):
    _script(tmp_path, "echo 'Complete Hysplit'\nexit 0\n")

    driver.run_hycs_std(tmp_path, timeout=5)  # does not raise


@pytest.mark.parametrize("stop", [KeyboardInterrupt, SystemExit])
@not_on_windows("the test needs POSIX permissions, process groups or signals")
def test_run_stops_hysplit_when_this_process_is_stopped(tmp_path, stop):
    """Ctrl-C or a worker told to exit ends hycs_std too, which has a session of its own."""
    import os
    import signal

    _script(tmp_path, "echo $$ > pid\nexec sleep 60\n")

    def interrupt(signum, frame):
        raise stop

    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        with pytest.raises(stop):
            driver.run_hycs_std(tmp_path)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "pid").read_text()), 0)


@not_on_windows("the test needs POSIX permissions, process groups or signals")
def test_terminate_escalates_when_group_kill_does_not_finish(monkeypatch):
    import signal
    import subprocess

    class FakeProc:
        pid = 1234

        def __init__(self) -> None:
            self.wait_calls = 0

        def wait(self, timeout: int) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired(cmd="hycs_std", timeout=timeout)
            return 0

    proc = FakeProc()
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        _process.os, "killpg", lambda pid, sig: killpg_calls.append((pid, sig))
    )

    _process.terminate(proc)  # type: ignore[arg-type]

    assert killpg_calls == [(proc.pid, signal.SIGTERM), (proc.pid, signal.SIGKILL)]
    assert proc.wait_calls == 2


# ---------------------------------------------------------------------------
# HysplitModel.run: what a finished hycs_std leaves behind
# ---------------------------------------------------------------------------


def _fake_hysplit(
    monkeypatch, *, log: str, rows: list[list[float]] | None, warning: str = ""
) -> list:
    """Replace hycs_std with one that writes *log*, *warning*, and particle *rows* (none: no file)."""
    calls: list[int | None] = []

    def fake_run(workdir: Path, timeout: int | None) -> None:
        calls.append(timeout)
        (workdir / "hysplit.log").write_text(log)
        if warning:
            (workdir / "WARNING").write_text(warning)
        if rows is not None:
            _write_particle_dat(workdir / PARTICLE_FILE, rows)

    monkeypatch.setattr(model, "run_hycs_std", fake_run)
    return calls


def _ending_at(minute: int) -> list[list[float]]:
    """
    Particle rows of one particle, written every hour, whose last row is *minute* before release.

    The particle is east of the test met's grid, so a run that stops early
    is a domain exit, not met that ran out.
    """
    times = [-1, *range(-60, -minute - 1, -60)]
    return [[t, 1, -100.0, 40.7, 10.0, 0.0] for t in times]


def _run(tmp_path, release, config=None):
    from runtraj.meteorology import run_window

    config = config or _config()
    make_met_files(tmp_path / "met", release.time, config.n_hours)
    met = make_met_config(tmp_path / "met")
    window = run_window(release.time, config.n_hours)
    (tmp_path / "run").mkdir(exist_ok=True)
    return HysplitModel().run(release, config, met, window, tmp_path / "run", timeout=5)


def test_a_run_whose_particles_left_the_domain_early_is_complete(
    tmp_path, point_release, monkeypatch
):
    """Particles that stop short with no met failure are a domain exit, kept for any model."""
    from runtraj.models import run_model

    _fake_hysplit(monkeypatch, log="", rows=_ending_at(13 * 60))
    make_met_files(tmp_path / "met", point_release.time, -24)
    met = make_met_config(tmp_path / "met")
    (tmp_path / "run").mkdir()

    run = run_model("hysplit", point_release, _config(), met, tmp_path / "run")

    assert run.result["age"].min() == -13 * 3600


def test_run_keeps_a_run_that_reaches_the_end_past_a_damaged_met_file(
    tmp_path, point_release, monkeypatch
):
    _fake_hysplit(
        monkeypatch,
        log=" WARNING metset: Only one time period of meteo data\n",
        rows=_ending_at(24 * 60),
    )

    assert _run(tmp_path, point_release).result["age"].min() == -24 * 3600


def test_run_leaves_an_empty_particle_file_to_the_caller(
    tmp_path, point_release, monkeypatch
):
    _fake_hysplit(
        monkeypatch,
        log=" WARNING metset: Only one time period of meteo data\n",
        rows=[],
    )

    assert _run(tmp_path, point_release).result.empty


def test_run_leaves_particles_that_stop_early_to_the_caller(
    tmp_path, point_release, monkeypatch
):
    """Particles that all left the met's domain before the end are a run."""
    _fake_hysplit(monkeypatch, log="", rows=_ending_at(13 * 60))

    assert _run(tmp_path, point_release).result["age"].min() == -13 * 3600


def test_a_run_with_zcfx_in_varsiwant_gives_a_zcfx_column(
    tmp_path, point_release, monkeypatch
):
    """`zcfx` is a column of the particles under that name."""
    from runtraj.models import run_model

    rows = [[*row, 2.5] for row in _ending_at(24 * 60)]
    _fake_hysplit(monkeypatch, log="", rows=rows)
    make_met_files(tmp_path / "met", point_release.time, -24)
    met = make_met_config(tmp_path / "met")
    (tmp_path / "run").mkdir()

    run = run_model(
        "hysplit",
        point_release,
        _config(varsiwant=[*VARS, "zcfx"]),
        met,
        tmp_path / "run",
    )

    assert run.result["zcfx"].eq(2.5).all()
    entries = setup_entries(_config(varsiwant=[*VARS, "zcfx"]))
    assert entries["varsiwant"][-1] == "zcfx"


def test_setup_cfg_writes_dswf_in_upper_case_and_the_other_names_as_they_are(
    tmp_path, point_release
):
    config = HysplitConfig(
        n_hours=-24, varsiwant=["time", "indx", "long", "lati", "zagl", "dswf", "zcfx"]
    )
    text = (_inputs(tmp_path, point_release, config) / "SETUP.CFG").read_text()
    assert "'time', 'indx', 'long', 'lati', 'zagl', 'DSWF', 'zcfx'," in text
    assert config.varsiwant[-2:] == ["dswf", "zcfx"]


def test_run_without_a_particle_file_fails(tmp_path, point_release, monkeypatch):
    _fake_hysplit(monkeypatch, log="", rows=None)

    with pytest.raises(SimulationError, match=PARTICLE_FILE) as caught:
        _run(tmp_path, point_release)
    assert caught.value.reason == FailureReason.NO_PARTICLE_DATA


PUFF_LIMIT = " WARNING emspnt: exceeding puff limit\n"


def test_a_multipoint_release_cut_short_by_maxpar_fails(
    tmp_path, multipoint_release, monkeypatch
):
    """HYSPLIT's puff-limit warning means the last locations got fewer particles."""
    _fake_hysplit(monkeypatch, log="", rows=_ending_at(24 * 60), warning=PUFF_LIMIT)

    with pytest.raises(SimulationError, match="3 release locations") as caught:
        _run(tmp_path, multipoint_release, _config(maxpar=10))
    assert caught.value.reason == FailureReason.PARTICLE_LIMIT
    assert "Raise maxpar or leave it unset" in str(caught.value)


def test_a_point_release_at_the_puff_limit_is_a_run(
    tmp_path, point_release, column_release, monkeypatch
):
    """From one location HYSPLIT asks for numpar + 1 and keeps numpar, which is harmless."""
    _fake_hysplit(monkeypatch, log="", rows=_ending_at(24 * 60), warning=PUFF_LIMIT)

    assert not _run(tmp_path / "point", point_release).result.empty
    assert not _run(tmp_path / "column", column_release).result.empty


def test_a_multipoint_release_without_the_warning_is_a_run(
    tmp_path, multipoint_release, monkeypatch
):
    _fake_hysplit(monkeypatch, log="", rows=_ending_at(24 * 60))

    assert not _run(tmp_path, multipoint_release).result.empty


def test_a_perturbed_run_is_one_hycs_std_call_with_winderr(
    tmp_path, point_release, monkeypatch
):
    # East of the test met's grid, so the run's early end is a domain exit.
    calls = _fake_hysplit(
        monkeypatch, log="", rows=[[-60, 1, -100.0, 40.7, 10.0, 1e-5]]
    )
    config = _config(siguverr=1.0, tluverr=60.0, zcoruverr=500.0, horcoruverr=40.0)

    result = _run(tmp_path, point_release, config)

    workdir = tmp_path / "run"
    assert calls == [5]
    assert (workdir / "WINDERR").exists()
    assert not (workdir / "ZIERR").exists()
    assert "winderrtf=1" in (workdir / "SETUP.CFG").read_text().lower()
    assert len(result.result) == 1


# ---------------------------------------------------------------------------
# check_met_stop: met that ran out, told from a domain exit
# ---------------------------------------------------------------------------


def _grid(nx: int = 50, ny: int = 50, west: float = 20.0, south: float = -10.0):
    """A 1 degree lat/lon grid of *nx* by *ny* points from (*west*, *south*)."""
    from arlmet import Grid, Projection

    projection = Projection(
        pole_lat=90.0,
        pole_lon=0.0,
        tangent_lat=1.0,
        tangent_lon=1.0,
        grid_size=0.0,
        orientation=0.0,
        cone_angle=0.0,
        sync_x=1.0,
        sync_y=1.0,
        sync_lat=south,
        sync_lon=west,
    )
    return Grid(projection=projection, nx=nx, ny=ny)


#: The middle of :func:`_grid`, 24.5 cells from each side.
MIDDLE = (44.5, 14.5)


def test_cells_from_edge_counts_grid_cells_to_the_nearest_side():
    lon = [44.5, 21.5, 66.0, 44.5]
    lat = [14.5, 14.5, 14.5, -8.0]

    cells = cells_from_edge(lon, lat, _grid())

    assert cells.tolist() == pytest.approx([24.5, 1.5, 3.0, 2.0])


def test_cells_from_edge_is_negative_outside_the_grid():
    assert cells_from_edge([10.0], [14.5], _grid())[0] < 0


def test_a_global_grid_has_no_east_or_west_side():
    """A point on the seam of a global grid is still inside it."""
    grid = _grid(nx=360, ny=181, west=0.0, south=-90.0)
    assert grid.wraps_lon

    cells = cells_from_edge([0.5], [0.0], grid)

    # Half a cell east of the seam and 90 cells from each pole.
    assert cells[0] == pytest.approx(90.0)


def _stopped_at(minute: float, *positions: tuple[float, float, float]):
    """
    Particles that each stop *minute* minutes back, at a (lon, lat, zagl) of *positions*.

    Each starts at the grid's middle and has a row at the release and one at
    the stop, as HYSPLIT writes every time step of each. ``age`` is in
    seconds, as :func:`read_particle_dat` gives it.
    """
    import pandas as pd

    rows = []
    for i, (lon, lat, zagl) in enumerate(positions, start=1):
        rows.append((i, -60.0, *MIDDLE, 10.0))
        rows.append((i, -60.0 * minute, lon, lat, zagl))
    return pd.DataFrame(rows, columns=["particle", "age", "lon", "lat", "zagl"])


def _check(particles, n_hours: int = -24, grid=None) -> None:
    check_met_stop(particles, n_hours, grid or _grid())


def test_a_run_the_met_stopped_inside_the_domain_fails():
    """HYSPLIT can stop every particle where the met ran out and report a complete run."""
    particles = _stopped_at(360, (*MIDDLE, 100.0), (40.0, 10.0, 500.0))

    with pytest.raises(SimulationError, match="the met ran out") as caught:
        _check(particles)

    assert caught.value.reason == FailureReason.MET_COVERAGE
    message = str(caught.value)
    assert "6.0 h into the 24 h run" in message
    assert "up to 24.5 grid cells inside" in message


@pytest.mark.parametrize(
    "position",
    [
        (21.5, 14.5, 10.0),  # 1.5 cells from the west side
        (67.5, 14.5, 10.0),  # 1.5 from the east side
        (44.5, -8.5, 10.0),  # 1.5 from the south side
        (44.5, 37.0, 10.0),  # 2.0 from the north side
    ],
)
def test_particles_that_stop_within_two_cells_of_an_edge_left_the_domain(position):
    """A particle that leaves the domain ends 1 to 2 cells from its edge."""
    _check(_stopped_at(13 * 60, position))


def test_one_particle_more_than_the_limit_inside_fails_the_run():
    inside = MET_STOP_CELLS + 1
    particles = _stopped_at(
        13 * 60, (21.5, 14.5, 10.0), (20.0 + inside, 14.5, 10.0), (67.5, 14.5, 10.0)
    )

    with pytest.raises(SimulationError) as caught:
        _check(particles)
    assert caught.value.reason == FailureReason.MET_COVERAGE


def test_a_particle_near_the_domain_top_still_fails_the_run():
    """HYSPLIT keeps particles from rising above the top, so the top cannot end a run."""
    with pytest.raises(SimulationError):
        _check(_stopped_at(13 * 60, (*MIDDLE, 24900.0)))


def test_only_the_particles_at_the_last_time_step_count():
    """A particle that stopped earlier inside the domain is not where the run stopped."""
    import pandas as pd

    at_the_edge = _stopped_at(13 * 60, (21.5, 14.5, 10.0))
    earlier_inside = _stopped_at(6 * 60, (*MIDDLE, 10.0)).assign(particle=2)

    _check(pd.concat([at_the_edge, earlier_inside]))


def test_a_run_that_reaches_n_hours_is_not_checked():
    _check(_stopped_at(24 * 60, (*MIDDLE, 10.0)))


def test_a_forward_run_is_checked_the_same():
    particles = _stopped_at(360, (*MIDDLE, 10.0)).assign(age=lambda df: df["age"].abs())

    with pytest.raises(SimulationError):
        _check(particles, n_hours=24)


def test_particles_without_positions_are_not_checked():
    _check(_stopped_at(360, (*MIDDLE, 10.0)).drop(columns="lat"))


def _rows_inside(minute: int) -> list[list[float]]:
    """Rows of VARS for one particle that stops *minute* back in the middle of :func:`_grid`."""
    return [[-1, 1, *MIDDLE, 10.0, 0.0], [-minute, 1, *MIDDLE, 10.0, 0.0]]


def test_run_fails_a_run_whose_particles_stopped_inside_the_met(
    tmp_path, point_release, monkeypatch
):
    _fake_hysplit(monkeypatch, log=" Complete Hysplit\n", rows=_rows_inside(360))
    monkeypatch.setattr(model, "_met_grid", lambda path: _grid())

    with pytest.raises(SimulationError, match="the met ran out") as caught:
        _run(tmp_path, point_release)
    assert caught.value.reason == FailureReason.MET_COVERAGE
    assert caught.value.log == " Complete Hysplit\n"


def test_run_does_not_check_where_particles_stopped_when_outdt_is_set(
    tmp_path, point_release, monkeypatch
):
    """With a row only every outdt minutes, a particle's last row can be far from where it left."""
    _fake_hysplit(monkeypatch, log="", rows=_rows_inside(360))
    monkeypatch.setattr(model, "_met_grid", lambda path: _grid())

    run = _run(tmp_path, point_release, _config(outdt=60))

    assert run.result["age"].min() == -360 * 60


def test_met_grid_is_the_grid_of_a_met_file(tmp_path):
    from ...fixtures.factories import write_arl_file

    grid = model._met_grid(write_arl_file(tmp_path / "met.arl"))

    assert (grid.nx, grid.ny) == (20, 20)


# ---------------------------------------------------------------------------
# write_inputs: SETUP.CFG
# ---------------------------------------------------------------------------


def _setup_cfg(tmp_path, release, config=None) -> str:
    return (_inputs(tmp_path, release, config) / "SETUP.CFG").read_text().lower()


def test_setup_cfg_holds_the_settings_and_kmsl(tmp_path, point_release):
    content = _setup_cfg(tmp_path, point_release)
    assert "numpar" in content
    assert "varsiwant" in content
    assert "kmsl=0" in content
    assert "seed=" not in content


def test_setup_cfg_includes_the_seed_when_configured(tmp_path, point_release):
    content = _setup_cfg(
        tmp_path, point_release, HysplitConfig(n_hours=-24, seed=17, krand=2)
    )
    assert "seed=-18" in content  # -(seed+1): the value HYSPLIT honours
    assert "winderrtf=0" in content


def test_setup_cfg_sets_winderrtf_from_error_settings(tmp_path, point_release):
    config = _config(siguverr=1.0, tluverr=60.0, zcoruverr=500.0, horcoruverr=40.0)
    assert "winderrtf=1" in _setup_cfg(tmp_path, point_release, config)


WIND = {"siguverr": 1.0, "tluverr": 60.0, "zcoruverr": 500.0, "horcoruverr": 40.0}
ZI = {"sigzierr": 20.0, "tlzierr": 60.0, "horcorzierr": 40.0}


@pytest.mark.parametrize(
    ("settings", "before", "after"),
    [
        ({}, 0, 0),
        (WIND, 1, 1),
        (ZI, 2, 2),
        (WIND | ZI, 3, 3),
        ({"ziscale": 0.8}, 0, -2),
        ({"ziscale": 0.8} | WIND, 1, -1),
        ({"ziscale": 0.8} | ZI, 2, 0),
        ({"ziscale": 0.8} | WIND | ZI, 3, 1),
    ],
)
def test_winderrtf_is_2_less_with_ziscale_from_hysplit_5_3_0(settings, before, after):
    # hycs_std 5.3.0 and later add 2 to WINDERRTF once they read ZICONTROL.
    config = _config(**settings)
    assert winderrtf(config, "hysplit.v5.2.3") == before
    assert winderrtf(config, "hysplit.v5.3.0") == after
    assert winderrtf(config, "hysplit.v5.4.2") == after


@pytest.mark.parametrize("version", [None, "unknown"])
def test_winderrtf_takes_a_version_without_a_number_as_older(version):
    assert winderrtf(_config(ziscale=0.8, **ZI), version) == 2


@pytest.mark.parametrize(
    ("version", "written"), [("hysplit.v5.2.3", 0), ("hysplit.v5.4.2", -2)]
)
def test_setup_cfg_winderrtf_follows_the_version_the_program_prints(
    tmp_path, point_release, version, written
):
    build = make_hysplit_dir(tmp_path / "hysplit", version)
    config = _config(directory=build, ziscale=0.8)
    assert f"winderrtf={written}" in _setup_cfg(tmp_path, point_release, config)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("hysplit.v5.2.3", False),
        ("hysplit.v5.3.0", True),
        ("hysplit.v5.4.2", True),
        ("hysplit.v6.0.0", True),
        ("unknown", False),
        (None, False),
    ],
)
def test_version_at_least_takes_a_version_without_a_number_as_the_oldest(
    version, expected
):
    assert version_at_least(version, (5, 3, 0)) is expected


CONCENTRATION = {
    "mode": "concentration",
    "pollutants": [{"name": "TRCA"}],
    "grids": [{"name": "g", "spacing_lat": 0.05, "spacing_lon": 0.05, "levels": [0]}],
}


def _points(*releases):
    return _release_points(releases)


def _refusals(tmp_path, release, version, **settings) -> list[str]:
    """Return the messages of the settings ``check_releases`` refuses on a build that prints *version*."""
    config = _config(
        directory=make_hysplit_dir(tmp_path / "hysplit", version), **settings
    )
    table = HysplitModel().check_releases(_release_points([release]), config)
    assert table["check"].eq("setting_refused").all()
    assert table["release"].isna().all()  # about the settings, not a release
    return table["message"].tolist()


@pytest.mark.parametrize("extra", [{}, {"ziscale": 0.8}])
def test_wind_error_in_the_concentration_mode_is_refused_from_hysplit_5_3_0(
    tmp_path, point_release, extra
):
    # With ziscale, WINDERRTF is -1 on 5.3+, which gets past HYSPLIT's check.
    found = _refusals(
        tmp_path, point_release, "hysplit.v5.4.2", **CONCENTRATION, **WIND, **extra
    )
    assert len(found) == 1
    assert "IDSP=2" in found[0]
    assert "hysplit.v5.4.2" in found[0]


@pytest.mark.parametrize(
    ("version", "settings"),
    [
        ("hysplit.v5.2.3", CONCENTRATION | WIND),  # applies it with IDSP=1
        ("hysplit.v5.4.2", CONCENTRATION),  # no wind error to drop
        ("hysplit.v5.4.2", CONCENTRATION | ZI),
        ("hysplit.v5.4.2", {"mode": "particle"} | WIND),  # IDSP=2
        (None, CONCENTRATION | WIND),  # no version: taken to be older
    ],
)
def test_wind_error_is_not_refused_elsewhere(
    tmp_path, point_release, version, settings
):
    assert _refusals(tmp_path, point_release, version, **settings) == []


@pytest.mark.parametrize("krand", [10, 11, 12, 13])
@pytest.mark.parametrize("version", ["hysplit.v5.0.2", None])
def test_krand_10_to_13_are_refused_before_hysplit_5_1_0(
    tmp_path, point_release, krand, version
):
    # 5.0.x runs them as krand=4; an unreadable version is taken to be older.
    (found,) = _refusals(tmp_path, point_release, version, krand=krand)
    assert f"krand={krand} is new in HYSPLIT 5.1.0" in found
    assert str(version) in found or "no version" in found


@pytest.mark.parametrize("version", ["hysplit.v5.1.0", "hysplit.v5.4.2"])
def test_krand_10_to_13_run_from_hysplit_5_1_0(tmp_path, point_release, version):
    assert _refusals(tmp_path, point_release, version, krand=10) == []


@pytest.mark.parametrize("krand", [None, 0, 1, 2, 3, 4])
def test_the_other_krand_values_run_on_hysplit_5_0(tmp_path, point_release, krand):
    assert _refusals(tmp_path, point_release, "hysplit.v5.0.2", krand=krand) == []


def test_the_version_is_asked_only_for_a_setting_that_needs_it(tmp_path, point_release):
    asked = tmp_path / "asked"
    build = make_hysplit_dir(tmp_path / "hysplit", "hysplit.v5.0.2", f"touch {asked}\n")
    HysplitModel().check_releases(_points(point_release), _config(directory=build))
    assert not asked.exists()


def test_the_check_has_nothing_to_refuse_with_no_program_to_ask(
    tmp_path, point_release
):
    config = _config(directory=tmp_path / "nothing", krand=10)
    assert HysplitModel().check_releases(_points(point_release), config).empty


@not_on_windows("the stand-in HYSPLIT is a shell script")
@pytest.mark.parametrize(
    "settings", [{"krand": 10}, CONCENTRATION | WIND], ids=["krand", "wind error"]
)
def test_a_refused_setting_fails_a_run_before_hycs_std_is_run_for_it(
    tmp_path, point_release, settings
):
    # The run asks for the version in an empty folder, where there is no CONTROL.
    ran = tmp_path / "ran"
    body = f"test -f CONTROL && touch {ran}\nexit 2\n"
    version = "hysplit.v5.0.2" if "krand" in settings else "hysplit.v5.4.2"
    build = make_hysplit_dir(tmp_path / "hysplit", version, body)
    with pytest.raises(SimulationError, match="HYSPLIT") as raised:
        run_model(
            "hysplit",
            point_release,
            _config(directory=build, **settings),
            make_met_config(tmp_path / "met"),
        )
    assert raised.value.reason == "RELEASE_REFUSED"
    assert not (tmp_path / "run").exists()
    assert not ran.exists()


def test_setup_cfg_takes_kmsl_from_an_msl_release(tmp_path, point_release):
    release = point_release.model_copy(
        update={"altitude": 1500.0, "altitude_ref": "msl"}
    )
    assert "kmsl=1" in _setup_cfg(tmp_path, release)


def test_read_namelist_reads_the_setup_cfg_written(tmp_path, point_release):
    from runtraj.models.hysplit.namelist import read_namelist

    config = HysplitConfig(n_hours=-24, numpar=150, wvert=True)
    entries = read_namelist(_inputs(tmp_path, point_release, config) / "SETUP.CFG")
    assert entries["numpar"] == 150
    assert entries["dzf"] == 0.01
    assert entries["wvert"] is True
    assert entries["efile"] == ""
    assert entries["varsiwant"] == [_upper_dswf(v) for v in config.varsiwant]
    assert entries["kmsl"] == 0


def test_read_namelist_rejects_a_file_that_is_not_one(tmp_path):
    from runtraj.models.hysplit.namelist import read_namelist

    path = tmp_path / "SETUP.CFG"
    path.write_text("NUMPAR=10,\n")
    with pytest.raises(ValueError, match=r"\$SETUP"):
        read_namelist(path)
    path.write_text("$SETUP\nNUMPAR 10\n$END\n")
    with pytest.raises(ValueError, match="not a KEY=value"):
        read_namelist(path)


def test_kmsl_comes_only_from_the_release():
    """KMSL follows each release's altitude_ref; there is no setting to contradict it."""
    with pytest.raises(ValueError, match="kmsl"):
        HysplitConfig(n_hours=-24, kmsl=0)


def test_write_inputs_takes_a_release_time_and_release_locations(tmp_path):
    """HYSPLIT's own inputs, with no release: CONTROL's first lines, KMSL and MAXPAR."""
    workdir = tmp_path / "run"
    write_inputs(
        workdir,
        release_time=dt.datetime(2021, 1, 5, 7, 37),
        locations=[(-111.9, 40.7, 1500.0), (-111.8, 40.8, 1600.0)],
        config=_config(),
        met_files=[tmp_path / "met" / "dummy"],
        kmsl=1,
    )

    assert (workdir / "CONTROL").read_text().splitlines()[:4] == [
        "21 01 05 07 37",
        "2",
        "40.7 -111.9 1500.0",
        "40.8 -111.8 1600.0",
    ]
    setup = (workdir / "SETUP.CFG").read_text().lower()
    assert "kmsl=1," in setup
    assert "maxpar=12," in setup  # two locations: 2 * (10 // 2 + 1)


def test_write_inputs_takes_heights_as_above_ground_unless_told(tmp_path):
    write_inputs(
        tmp_path / "run",
        dt.datetime(2021, 1, 5, 7, 37),
        [(40.7, -111.9, 10.0)],
        _config(),
        [tmp_path / "met" / "dummy"],
    )
    assert "kmsl=0," in (tmp_path / "run" / "SETUP.CFG").read_text().lower()


# ---------------------------------------------------------------------------
# write_inputs: CONTROL and the error and mixed-layer files
# ---------------------------------------------------------------------------


def test_inputs_hold_control_setup_and_the_binary(tmp_path, point_release):
    workdir = _inputs(tmp_path, point_release)

    assert (workdir / "CONTROL").exists()
    assert (workdir / "SETUP.CFG").exists()
    if sys.platform != "win32":  # where a link is refused, a copy stands in
        assert program_path(workdir).is_symlink()
    for name in ("WINDERR", "ZIERR", "ZICONTROL"):
        assert not (workdir / name).exists()


def test_winderr_lines_follow_the_settings_order(tmp_path, point_release):
    config = _config(siguverr=1.0, tluverr=60.0, zcoruverr=500.0, horcoruverr=40.0)
    lines = (_inputs(tmp_path, point_release, config) / "WINDERR").read_text()
    assert lines.split() == ["1.0", "60.0", "500.0", "40.0"]


def test_zierr_lines_follow_the_settings_order(tmp_path, point_release):
    config = _config(sigzierr=0.6, tlzierr=60.0, horcorzierr=40.0)
    lines = (_inputs(tmp_path, point_release, config) / "ZIERR").read_text()
    assert lines.split() == ["0.6", "60.0", "40.0"]


def test_zicontrol_holds_the_hourly_factors(tmp_path, point_release):
    config = _config(ziscale=[0.8, 0.8, 0.9])
    zicontrol = _inputs(tmp_path, point_release, config) / "ZICONTROL"
    assert zicontrol.read_text().strip().splitlines() == ["3", "0.8", "0.8", "0.9"]


def test_zicontrol_repeats_a_scalar_over_the_run(tmp_path, point_release):
    config = _config(n_hours=-4, ziscale=0.8)
    lines = (_inputs(tmp_path, point_release, config) / "ZICONTROL").read_text()
    assert lines.strip().splitlines() == ["4", "0.8", "0.8", "0.8", "0.8"]


# ---------------------------------------------------------------------------
# write_inputs: the HYSPLIT build and its data tables
# ---------------------------------------------------------------------------


def test_directory_is_used(tmp_path, point_release):
    build = _fake_build(tmp_path / "hysplit", "patched")
    workdir = _inputs(tmp_path, point_release, _config(directory=build))
    assert program_path(workdir).read_text() == "patched"


def test_without_directory_the_hycs_std_on_path_is_used(
    tmp_path, point_release, stand_in_hycs_std
):
    workdir = _inputs(tmp_path, point_release)
    assert (
        program_path(workdir).read_bytes()
        == program_path(stand_in_hycs_std).read_bytes()
    )


def test_directory_comes_before_path(tmp_path, monkeypatch):
    build = _fake_build(tmp_path / "hysplit")
    elsewhere = _fake_build(tmp_path / "elsewhere")
    monkeypatch.setenv("PATH", str(elsewhere / "exec"))
    assert driver.find_hycs_std(build) == program_path(build / "exec")
    assert (
        driver.find_hycs_std().resolve() == program_path(elsewhere / "exec").resolve()
    )


def test_without_directory_or_hycs_std_on_path_says_what_to_set(
    tmp_path, point_release, monkeypatch
):
    """HYSPLIT is not included: the message names both places to put it."""
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(HYSPLITNotFoundError, match="directory.*PATH"):
        _inputs(tmp_path, point_release)


def test_directory_without_exec_hycs_std_raises(tmp_path, point_release):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(HYSPLITNotFoundError, match="No exec/hycs_std in"):
        _inputs(tmp_path, point_release, _config(directory=empty))


@not_on_windows("the test needs POSIX permissions, process groups or signals")
def test_a_hycs_std_that_is_not_executable_says_so(tmp_path, point_release):
    """A HYSPLIT folder unpacked on Windows loses the executable bit."""
    build = _fake_build(tmp_path / "hysplit")
    (build / "exec" / "hycs_std").chmod(0o644)
    with pytest.raises(HYSPLITNotFoundError, match="not executable.*chmod \\+x"):
        _inputs(tmp_path, point_release, _config(directory=build))


def test_only_hycs_std_is_linked_from_exec(tmp_path, point_release):
    # A real HYSPLIT exec/ directory holds dozens of other programs.
    build = _fake_build(tmp_path / "hysplit")
    (build / "exec" / "Makefile").write_text("")
    (build / "exec" / "concplot").write_text("")
    workdir = _inputs(tmp_path, point_release, _config(directory=build))
    assert program_path(workdir).exists()
    assert not (workdir / "Makefile").exists()
    assert not (workdir / "concplot").exists()
    assert "directory" not in (workdir / "SETUP.CFG").read_text().lower()


def test_the_tables_are_linked_from_bdyfiles(tmp_path, point_release):
    """The four tables of bdyfiles/ are linked; nothing else in it is."""
    build = _fake_build(tmp_path / "hysplit")
    (build / "bdyfiles" / "Makefile").write_text("")

    workdir = _inputs(tmp_path, point_release, _config(directory=build))

    for name in ("LANDUSE.ASC", "ROUGLEN.ASC", "TERRAIN.ASC"):
        assert (workdir / name).resolve() == (build / "bdyfiles" / name).resolve()
    assert not (workdir / "Makefile").exists()


def test_hysplits_ascdata_cfg_is_written_to_read_the_tables_in_the_workdir(
    tmp_path, point_release
):
    """HYSPLIT's own ASCDATA.CFG says ../bdyfiles/; the workdir's says ./ and keeps the rest."""
    build = _fake_build(tmp_path / "hysplit")
    theirs = (build / "bdyfiles" / "ASCDATA.CFG").read_text().splitlines(True)
    assert "../bdyfiles/" in theirs[5]

    workdir = _inputs(tmp_path, point_release, _config(directory=build))

    written = (workdir / "ASCDATA.CFG").read_text().splitlines(keepends=True)
    assert not (workdir / "ASCDATA.CFG").is_symlink()
    assert written[:5] == theirs[:5]
    assert written[5].startswith("'./'")


def test_a_missing_table_says_where_the_tables_go(tmp_path, point_release):
    build = _fake_build(tmp_path / "hysplit")
    (build / "bdyfiles" / "TERRAIN.ASC").unlink()
    with pytest.raises(HYSPLITNotFoundError, match="No TERRAIN.ASC in"):
        _inputs(tmp_path, point_release, _config(directory=build))


def test_winderrtf_all_none():
    assert winderrtf(HysplitConfig(n_hours=-24)) == 0


def test_winderrtf_xy_only():
    e = HysplitConfig(
        n_hours=-24, siguverr=1.0, tluverr=60.0, zcoruverr=500.0, horcoruverr=40.0
    )
    assert winderrtf(e) == 1


def test_winderrtf_zi_only():
    e = HysplitConfig(n_hours=-24, sigzierr=0.6, tlzierr=60.0, horcorzierr=40.0)
    assert winderrtf(e) == 2


def test_winderrtf_both():
    e = HysplitConfig(
        n_hours=-24,
        siguverr=1.0,
        tluverr=60.0,
        zcoruverr=500.0,
        horcoruverr=40.0,
        sigzierr=0.6,
        tlzierr=60.0,
        horcorzierr=40.0,
    )
    assert winderrtf(e) == 3


def test_winderrtf_zero_value_params():
    """0.0 error params are set (not None) - winderrtf must still be 1."""
    e = HysplitConfig(
        n_hours=-24, siguverr=0.0, tluverr=0.0, zcoruverr=0.0, horcoruverr=0.0
    )
    assert winderrtf(e) == 1


def test_unset_maxpar_gives_every_location_its_rounded_up_share():
    """HYSPLIT gives each of n locations ``numpar // n``, often plus one."""
    p = HysplitConfig(n_hours=-24, numpar=1000)
    assert p.maxpar is None
    assert p.maxpar_for(1) == 1000  # one location keeps numpar
    assert p.maxpar_for(3) == 3 * 334
    assert p.maxpar_for(40) == 40 * 26
    assert p.maxpar_for(56) == 56 * 18
    assert (
        HysplitConfig(n_hours=-24, numpar=10).maxpar_for(40) == 40
    )  # at least one each


def test_unset_maxpar_covers_every_time_step_of_a_long_release():
    """HYSPLIT rounds each location's share up at each step: 12 x 3 x 30 particles."""
    p = HysplitConfig(n_hours=-6, numpar=1000, emisshrs=0.5)
    assert p.maxpar_for(1) == 1000  # one location keeps numpar
    assert p.maxpar_for(3) == 12 * 3 * 30
    two_sizes = HysplitConfig(n_hours=-6, numpar=1000, emisshrs=0.5, nbptyp=2)
    assert two_sizes.maxpar_for(3) == 6 * 2 * 3 * 30  # the size bins share numpar
    capped = HysplitConfig(n_hours=-6, numpar=1000, emisshrs=0.5, maxpar=900)
    assert capped.maxpar_for(3) == 900


def test_a_set_maxpar_is_used_as_given():
    p = HysplitConfig(n_hours=-24, numpar=1000, maxpar=800)
    assert p.maxpar_for(1) == p.maxpar_for(40) == 800
    assert setup_entries(p)["maxpar"] == 800
    assert "maxpar" not in setup_entries(HysplitConfig(n_hours=-24, numpar=1000))


def test_setup_cfg_maxpar_follows_the_releases_locations(
    tmp_path, point_release, column_release, multipoint_release
):
    """One location (a column is one line source) gets numpar; three get 3 * (10 // 3 + 1)."""
    for release, maxpar in [
        (point_release, 10),
        (column_release, 10),
        (multipoint_release, 12),
    ]:
        assert driver.release_locations(release.coords()) == (3 if maxpar == 12 else 1)
        assert f"maxpar={maxpar}," in _setup_cfg(tmp_path / release.kind, release)
    set_maxpar = _config(maxpar=10)
    assert "maxpar=10," in _setup_cfg(tmp_path / "set", multipoint_release, set_maxpar)


def test_zicontroltf_is_derived_from_ziscale():
    assert zicontroltf(HysplitConfig(n_hours=-24)) == 0
    assert ziscale_factors(HysplitConfig(n_hours=-24)) is None
    assert zicontroltf(HysplitConfig(n_hours=-24, ziscale=[1.0, 1.0])) == 0
    assert zicontroltf(HysplitConfig(n_hours=-24, ziscale=0.8)) == 1
    assert zicontroltf(HysplitConfig(n_hours=-24, ziscale=[1.0, 0.9])) == 1


def test_ziscale_scalar_repeats_for_every_hour_and_list_is_used_as_given():
    assert ziscale_factors(HysplitConfig(n_hours=-3, ziscale=0.8)) == [0.8, 0.8, 0.8]
    assert ziscale_factors(HysplitConfig(n_hours=-24, ziscale=[0.8])) == [0.8]
    assert ziscale_factors(HysplitConfig(n_hours=-24, ziscale=[[0.8, 0.9]])) == [
        0.8,
        0.9,
    ]


def test_setup_entries_write_the_derived_zicontroltf():
    assert setup_entries(HysplitConfig(n_hours=-24))["zicontroltf"] == 0
    assert setup_entries(HysplitConfig(n_hours=-24, ziscale=1.2))["zicontroltf"] == 1


def test_ziscale_at_most_150_hourly_factors():
    assert len(ziscale_factors(HysplitConfig(n_hours=-150, ziscale=0.8))) == 150
    assert zicontroltf(HysplitConfig(n_hours=-240, ziscale=[0.8] * 150)) == 1
    with pytest.raises(ValueError, match="at most 150"):
        HysplitConfig(n_hours=-151, ziscale=0.8)
    with pytest.raises(ValueError, match="at most 150"):
        HysplitConfig(n_hours=-24, ziscale=[0.8] * 151)


def test_long_run_without_scaling_is_fine():
    assert zicontroltf(HysplitConfig(n_hours=-240)) == 0


def test_setup_entries_route_transport_params_to_setup_cfg():
    p = HysplitConfig(n_hours=-24)
    entries = setup_entries(p)

    assert entries["numpar"] == p.numpar
    assert entries["varsiwant"] == [_upper_dswf(v) for v in p.varsiwant]
    assert entries["ichem"] == 8
    assert entries["idsp"] == 2
    # CONTROL / ZICONTROL / WINDERR fields never appear in SETUP.CFG
    for name in ("n_hours", "emisshrs", "w_option", "z_top", "ziscale", "siguverr"):
        assert name not in entries
    # None-valued fields are omitted
    assert "seed" not in entries
    assert "maxpar" not in entries  # write_inputs adds it for the release


# Fortran type of every SETUP.CFG entry runtraj writes, from the SETUP
# namelist declarations in HYSPLIT's hysetup.f.

HYSPLIT_SETUP_TYPES = {
    **dict.fromkeys(
        [
            "capemin", "delt", "dxf", "dyf", "dzf", "frhmax", "frhs", "frme",
            "frmr", "frts", "frvs", "hscale", "p10f", "qcycle", "splitf",
            "tkerd", "tkern", "tlfrac", "tratio", "tvmix", "veght", "vscale",
            "vscales", "vscaleu", "wbbh", "wbwf", "wbwr",
        ],
        "REAL",
    ),
    **dict.fromkeys(
        [
            "cmass", "conage", "cpack", "ichem", "idsp", "initd", "k10m",
            "kagl", "kbls", "kblt", "kdef", "khinp", "khmax", "kmix0", "kmixd",
            "kpuff", "krand", "krnd", "kspl", "kwet", "kzmix", "maxdim",
            "maxpar", "mgmin", "mhrs", "nbptyp", "ncycl", "ndump", "ninit",
            "nstr", "numpar", "nturb", "nver", "outdt", "rhb", "rht", "seed",
            "tout", "zicontroltf",
        ],
        "INTEGER",
    ),
    **dict.fromkeys(["efile", "pinbc", "pinpf", "poutf", "varsiwant"], "CHARACTER"),
    "wvert": "LOGICAL",
}  # fmt: skip


def test_hysplit_setup_types_cover_every_setup_entry():
    config = HysplitConfig(
        n_hours=-24,
        capemin=-1,
        kblt=5,
        kmixd=3,
        vscales=-1,
        cpack=1,
        seed=1,
        krand=2,
        maxpar=1000,
    )
    assert set(setup_entries(config)) == set(HYSPLIT_SETUP_TYPES)


def test_settings_hysplit_chooses_are_unset_and_left_out_of_setup_cfg():
    # HYSPLIT sets these after reading SETUP.CFG, from its mode and numpar.
    entries = setup_entries(HysplitConfig(n_hours=-24))

    assert not {"capemin", "cpack", "kblt", "kmixd", "krand", "vscales"} & set(entries)
    assert (entries["numpar"], entries["wvert"]) == (2500, True)
    assert entries["varsiwant"] == [_upper_dswf(v) for v in HYSPLIT_VARSIWANT]


@pytest.mark.parametrize(
    "name",
    [
        n
        for n, t in HYSPLIT_SETUP_TYPES.items()
        if t == "REAL" and n in HysplitConfig.model_fields
    ],
)
def test_real_setup_fields_accept_fractions(name):
    # HYSPLIT reads these as REAL, so a fractional value is valid.
    assert setup_entries(HysplitConfig(n_hours=-24, **{name: 0.5}))[name] == 0.5


@pytest.mark.parametrize(
    "name",
    [
        n
        for n, t in HYSPLIT_SETUP_TYPES.items()
        if t == "INTEGER" and n in HysplitConfig.model_fields
    ],
)
def test_integer_setup_fields_reject_fractions(name):
    # HYSPLIT stops with a namelist read error on a fractional INTEGER.
    with pytest.raises(ValidationError, match=name):
        HysplitConfig(n_hours=-24, **{name: 0.5})


def test_fixed_setup_entries_are_written_and_are_no_settings():
    entries = setup_entries(HysplitConfig(n_hours=-24))
    for name, value in FIXED_SETUP.items():
        assert entries[name] == value
    assert "idsp" not in HysplitConfig.model_fields


def test_idsp_is_refused_as_a_setting_and_ichem_is_8_or_unset():
    # Only ICHEM=8 with IDSP=2 writes particles.
    assert FIXED_SETUP == {"ichem": 8, "idsp": 2}
    with pytest.raises(ValidationError, match="idsp"):
        HysplitConfig(n_hours=-24, idsp=2)
    with pytest.raises(ValidationError, match="ichem=0"):
        HysplitConfig(n_hours=-24, ichem=0)
    assert setup_entries(HysplitConfig(n_hours=-24, ichem=8))["ichem"] == 8


def test_setup_entries_default_to_hysplits_own():
    # SETUP_INIDSP in HYSPLIT's hysetup.f: the defaults of a dispersion run.
    hysplit = {
        "cmass": 0, "conage": 24, "delt": 0.0, "dxf": 1.0, "dyf": 1.0,
        "dzf": 0.01, "efile": "", "frhmax": 3.0, "frhs": 1.0, "frme": 0.1,
        "frmr": 0.0, "frts": 0.1, "frvs": 0.01, "initd": 0, "kagl": 0,
        "khinp": 0, "khmax": 9999, "kpuff": 0, "krnd": 6, "kspl": 1,
        "kwet": 1, "maxdim": 1, "mhrs": 0, "nbptyp": 1, "ncycl": 0,
        "ndump": 0, "ninit": 1, "nstr": 0, "nver": 0, "p10f": 1.0,
        "pinbc": "PARINBC", "pinpf": "PARINIT", "poutf": "PARDUMP",
        "rhb": 80, "rht": 60, "splitf": 1.0, "tout": 0, "tratio": 0.75,
    }  # fmt: skip
    entries = setup_entries(HysplitConfig(n_hours=-24))
    assert {name: entries[name] for name in hysplit} == hysplit


def test_setup_cfg_can_hold_the_entries_it_held_when_they_were_fixed():
    # Every SETUP.CFG entry and value from when most of them were fixed,
    # given as settings, with a seed and maxpar.
    before = {
        "veght": 0.5, "numpar": 200, "capemin": -1, "cmass": 0, "conage": 48,
        "cpack": 1, "delt": 1, "dxf": 1, "dyf": 1, "dzf": 0.01, "efile": "",
        "frhmax": 3, "frhs": 1, "frme": 0.1, "frmr": 0, "frts": 0.1,
        "frvs": 0.01, "hscale": 10800, "ichem": 8, "idsp": 2, "initd": 0,
        "k10m": 1, "kagl": 1, "kbls": 1, "kblt": 5, "kdef": 0, "khinp": 0,
        "khmax": 9999, "kmix0": 150, "kmixd": 3, "kpuff": 0, "krand": 2,
        "krnd": 6, "kspl": 1, "kwet": 1, "kzmix": 0, "maxdim": 1,
        "maxpar": 1000, "mgmin": 10, "mhrs": 9999, "nbptyp": 1, "ncycl": 0,
        "ndump": 0, "ninit": 1, "nstr": 0, "nturb": 0, "nver": 0, "outdt": 0,
        "p10f": 1, "pinbc": "", "pinpf": "", "poutf": "", "qcycle": 0,
        "rhb": 80, "rht": 60, "seed": -4, "splitf": 1, "tkerd": 0.18,
        "tkern": 0.18, "tlfrac": 0.1, "tout": 0, "tratio": 0.75, "tvmix": 1,
        "vscale": 200, "vscaleu": 200, "vscales": -1, "wbbh": 0, "wbwf": 0,
        "wbwr": 0, "wvert": False, "zicontroltf": 0,
    }  # fmt: skip
    config = HysplitConfig(
        n_hours=-24,
        numpar=200,
        capemin=-1,
        kblt=5,
        kmixd=3,
        vscales=-1,
        wvert=False,
        seed=3,
        krand=2,
        maxpar=1000,
        conage=48,
        cpack=1,
        delt=1.0,
        kagl=1,
        mhrs=9999,
        pinbc="",
        pinpf="",
        poutf="",
    )
    entries = setup_entries(config)
    entries.pop("varsiwant")
    assert entries == before


def test_setup_entries_map_seed_to_negative_namelist_value():
    # HYSPLIT's ran1 re-initializes only from a negative value; -(seed+1)
    # keeps every seed distinct and off the unseeded default (state 1).
    assert setup_entries(HysplitConfig(n_hours=-24, seed=17, krand=2))["seed"] == -18
    assert setup_entries(HysplitConfig(n_hours=-24, seed=0, krand=2))["seed"] == -1
    assert setup_seed(42) == -43


def test_every_realization_of_a_seed_gets_its_own_namelist_value():
    config = HysplitConfig(n_hours=-24, seed=0, krand=2)
    seeds = [setup_entries(r)["seed"] for r in config.realizations(4)]
    assert seeds == [-1, -2, -3, -4]


def test_the_driver_names_only_real_settings_outside_setup_cfg():
    """
    Every setting the driver keeps out of SETUP.CFG exists.

    A setting left off that list goes to SETUP.CFG, where
    test_hysplit_setup_types_cover_every_setup_entry catches one HYSPLIT
    does not know.
    """
    from runtraj.models.hysplit.driver import NOT_IN_SETUP

    assert set(HysplitConfig.model_fields) >= NOT_IN_SETUP
    entries = setup_entries(HysplitConfig(n_hours=-24))
    assert "numpar" in entries
    assert not NOT_IN_SETUP & set(entries)


# -- Windows -------------------------------------------------------------------
# These run on every platform: a stand-in for ``sys`` says it is Windows to the
# driver only, so the Linux and macOS jobs test what the Windows job runs.


def _windows(monkeypatch) -> None:
    """Make the driver, and only the driver, see Windows."""
    monkeypatch.setattr(driver, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")


def test_on_windows_a_program_is_found_by_its_extension(tmp_path, monkeypatch):
    _windows(monkeypatch)
    (tmp_path / "hycs_std.exe").write_text("")
    assert driver.program_file(tmp_path, "hycs_std") == tmp_path / "hycs_std.exe"
    assert driver.program_file(tmp_path, "hyts_std") == tmp_path / "hyts_std"


@not_on_windows("the test renames the stand-in program to a .exe")
def test_on_windows_a_hysplit_folder_holds_hycs_std_exe(tmp_path, monkeypatch):
    build = make_hysplit_dir(tmp_path / "hysplit")
    (build / "exec" / "hycs_std").rename(build / "exec" / "hycs_std.exe")
    _windows(monkeypatch)
    assert driver.find_hycs_std(build) == build / "exec" / "hycs_std.exe"
    (build / "exec" / "hycs_std.exe").unlink()
    with pytest.raises(HYSPLITNotFoundError, match="No exec/hycs_std in"):
        driver.find_hycs_std(build)


@not_on_windows("the test renames the stand-in program to a .exe")
def test_where_windows_refuses_a_link_the_inputs_get_a_copy(
    tmp_path, point_release, monkeypatch
):
    build = make_hysplit_dir(tmp_path / "hysplit")
    (build / "exec" / "hycs_std").rename(build / "exec" / "hycs_std.exe")
    _windows(monkeypatch)

    def refused(self, target, target_is_directory=False):
        raise OSError("A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", refused)
    workdir = _inputs(tmp_path, point_release, _config(directory=build))

    program = workdir / "hycs_std.exe"
    assert program.is_file() and not program.is_symlink()
    # A hard link: the same file, so a run takes no space for it.
    assert program.samefile(build / "exec" / "hycs_std.exe")
    assert not (workdir / "ASCDATA.CFG").is_symlink()
    assert (workdir / "TERRAIN.ASC").read_text() == STAND_IN_TABLES["TERRAIN.ASC"]


def test_on_windows_a_file_on_another_drive_is_copied(tmp_path, monkeypatch):
    _windows(monkeypatch)

    def refused(*args, **kwargs):
        raise OSError("refused")

    monkeypatch.setattr(Path, "symlink_to", refused)
    monkeypatch.setattr(driver.os, "link", refused)  # as across drives
    (tmp_path / "table").write_text("values")
    driver._link(tmp_path / "copy", tmp_path / "table")

    assert (tmp_path / "copy").read_text() == "values"
    assert not (tmp_path / "copy").samefile(tmp_path / "table")


def test_a_refused_link_is_an_error_off_windows(tmp_path, monkeypatch):
    def refused(self, target, target_is_directory=False):
        raise OSError("refused")

    monkeypatch.setattr(Path, "symlink_to", refused)
    with pytest.raises(OSError, match="refused"):
        driver._link(tmp_path / "link", tmp_path)


def test_hysplits_input_files_have_unix_line_endings(tmp_path, point_release):
    workdir = _inputs(tmp_path, point_release)
    for name in ("CONTROL", "SETUP.CFG"):
        assert b"\r" not in (workdir / name).read_bytes()


def test_the_met_folder_of_control_has_a_forward_slash(tmp_path):
    ControlFile(
        release_time=dt.datetime(2021, 1, 15, 6),
        locations=[(-112.0, 40.5, 5.0)],
        emisshrs=0.01,
        n_hours=-6,
        w_option=0,
        z_top=10000.0,
        met_files=[tmp_path / "met" / "hrrr.arl"],
    ).write(tmp_path / "CONTROL")
    lines = (tmp_path / "CONTROL").read_text().splitlines()
    assert lines[lines.index("hrrr.arl") - 1] == (tmp_path / "met").as_posix() + "/"


def test_a_running_process_is_stopped():
    """HYSPLIT runs in a group of its own, which :func:`terminate` ends: a session on POSIX, a process group on Windows."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], **_process.own_group()
    )
    _process.terminate(proc)
    assert proc.poll() is not None

"""The recorded HYSPLIT runs' script (``tests/fixtures/replay.py``), on a stand-in HYSPLIT."""

import lzma
import subprocess
from pathlib import Path

import pytest

from runtraj.models.hysplit import driver
from tests.fixtures import replay

#: HYSPLIT's particle file.
PARTICLES = driver.PARTICLE_FILE

#: A ``CONTROL`` with a met folder, as runtraj writes one (shortened).
CONTROL = "21 01 15 06 00\n1\n40.5 -112.0 5.0\n-6\n0\n10000.0\n1 1\n{met}/\nhrrr.arl\n"


def _real_hysplit(tmp_path: Path) -> Path:
    """A HYSPLIT folder whose ``hycs_std`` prints a version, writes three files and exits 3."""
    folder = tmp_path / "real"
    (folder / "exec").mkdir(parents=True)
    (folder / "bdyfiles").mkdir()
    for table in replay.DATA_TABLES:
        (folder / "bdyfiles" / table).write_text("a table\n")
    program = folder / "exec" / "hycs_std"
    program.write_text(
        "#!/bin/sh\n"
        "echo ' HYSPLIT version: hysplit.v9.9.9'\n"
        "[ -f CONTROL ] || exit 2\n"
        f"wc -c < CONTROL > {PARTICLES}\n"
        "echo done > MESSAGE\n"
        "echo a dump runtraj never reads > PARTICLE.DAT\n"
        "exit 3\n"
    )
    program.chmod(0o755)
    return folder


def _call(exec_dir: Path, workdir: Path) -> subprocess.CompletedProcess:
    """Run the folder's ``hycs_std`` in *workdir*, as runtraj does (through a link)."""
    (workdir / "hycs_std").symlink_to(exec_dir / "hycs_std")
    return subprocess.run(
        [str(workdir / "hycs_std")], cwd=workdir, capture_output=True, text=True
    )


def _workdir(tmp_path: Path, name: str, met: str) -> Path:
    workdir = tmp_path / name
    workdir.mkdir()
    (workdir / "CONTROL").write_text(CONTROL.format(met=met))
    (workdir / "SETUP.CFG").write_text("&SETUP\n numpar = 100,\n/\n")
    (workdir / "ASCDATA.CFG").write_text("this machine's tables\n")
    (workdir / replay.LOG_FILE).write_text("")
    return workdir


def test_the_log_is_the_one_runtraj_writes():
    assert replay.LOG_FILE == driver.LOG_FILE


def test_a_met_folder_is_compared_as_a_placeholder():
    a = replay.normalize("CONTROL", CONTROL.format(met="/here/met").encode())
    b = replay.normalize("CONTROL", CONTROL.format(met="/there/other").encode())
    assert a == b
    assert b"<met>/\nhrrr.arl" in a
    assert replay.normalize("SETUP.CFG", b"/a/\n") == b"/a/\n"


def test_a_recorded_run_replays_on_another_machine(tmp_path):
    cases = tmp_path / "cases"
    recording = replay.make_folder(tmp_path / "rec", cases, _real_hysplit(tmp_path))
    first = _call(recording, _workdir(tmp_path, "one", "/data/met"))
    assert first.returncode == 3
    assert "hysplit.v9.9.9" in first.stdout
    (case,) = (cases / "hycs_std").iterdir()
    assert sorted(p.name for p in (case / "inputs").iterdir()) == [
        "CONTROL",
        "SETUP.CFG",
    ]
    assert sorted(p.name for p in (case / "outputs").iterdir()) == [
        "MESSAGE.xz",
        f"{PARTICLES}.xz",
    ]

    replaying = replay.make_folder(tmp_path / "rep", cases)
    assert (replaying.parent / "bdyfiles" / "ASCDATA.CFG").is_file()
    workdir = _workdir(tmp_path, "two", "/elsewhere/met_cache")
    second = _call(replaying, workdir)
    assert (second.returncode, second.stdout) == (first.returncode, first.stdout)
    recorded = lzma.decompress((case / "outputs" / f"{PARTICLES}.xz").read_bytes())
    assert (workdir / PARTICLES).read_bytes() == recorded
    assert (workdir / "MESSAGE").read_text() == "done\n"


def test_the_version_call_replays(tmp_path):
    cases = tmp_path / "cases"
    recording = replay.make_folder(tmp_path / "rec", cases, _real_hysplit(tmp_path))
    (tmp_path / "a").mkdir()
    assert _call(recording, tmp_path / "a").returncode == 2
    (tmp_path / "b").mkdir()
    replayed = _call(replay.make_folder(tmp_path / "rep", cases), tmp_path / "b")
    assert replayed.returncode == 2
    assert "hysplit.v9.9.9" in replayed.stdout


def test_inputs_with_no_recording_fail_and_are_kept(tmp_path):
    cases = tmp_path / "cases"
    recording = replay.make_folder(tmp_path / "rec", cases, _real_hysplit(tmp_path))
    _call(recording, _workdir(tmp_path, "one", "/data/met"))
    misses = tmp_path / "misses"
    replaying = replay.make_folder(tmp_path / "rep", cases, misses=misses)
    workdir = _workdir(tmp_path, "two", "/data/met")
    (workdir / "SETUP.CFG").write_text("&SETUP\n numpar = 200,\n/\n")
    done = _call(replaying, workdir)
    assert done.returncode == replay.MISS_STATUS
    assert "no recorded hycs_std run has these inputs" in done.stdout
    (kept,) = replay.misses(misses)
    assert "numpar = 200" in (kept / "SETUP.CFG").read_text()
    assert not (workdir / PARTICLES).exists()


def test_a_replay_folder_has_only_the_programs_recorded(tmp_path):
    cases = tmp_path / "cases"
    (cases / "hycs_std").mkdir(parents=True)
    exec_dir = replay.make_folder(tmp_path / "rep", cases)
    assert [p.name for p in exec_dir.iterdir()] == ["hycs_std"]


def test_builds_are_read_from_the_file(tmp_path, monkeypatch):
    real = _real_hysplit(tmp_path)
    config = tmp_path / "hysplit-builds.toml"
    config.write_text(f'[builds]\n"9.9.9" = "{real}"\n')
    monkeypatch.setenv("RUNTRAJ_HYSPLIT_BUILDS", str(config))
    assert replay.builds([]) == [("9.9.9", real)]
    with pytest.raises(SystemExit, match="lists no build of HYSPLIT 1.0"):
        replay.builds(["1.0"])
    monkeypatch.setenv("RUNTRAJ_HYSPLIT_BUILDS", str(tmp_path / "none.toml"))
    with pytest.raises(SystemExit, match=r"\[builds\]"):
        replay.builds([])

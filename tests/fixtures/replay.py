"""
Recorded HYSPLIT runs: run a real HYSPLIT build once and record it, then replay the recording.

GitHub's runners have no HYSPLIT (runtraj publishes none, and NOAA ARL's
builds are not ours to download there), so CI replays runs recorded where a
build is licensed. A *case* is one call of a HYSPLIT program: the files
runtraj gave it (``CONTROL``, ``SETUP.CFG``, ...) and what it gave back (its
output files, what it printed, its exit status). Cases are kept under
``tests/recorded/hysplit/<version>/<program>/<key>/``, where the key is a hash
of the inputs.

This file makes a HYSPLIT folder (``exec/`` and ``bdyfiles/``) whose programs
are small scripts that call this file again, in one of two modes:

``record``
    Run the real program of a HYSPLIT build, then save the case.
``replay``
    Find the case with the same inputs and write its output files, print
    what it printed and exit with its status. Inputs with no case are a
    *miss*: the program says so and exits with :data:`MISS_STATUS`, so the
    run fails, and the inputs are kept in the folder's ``misses/``.

Inputs are compared after one change: a met folder in ``CONTROL`` (an
absolute path) is written as ``<met>/``, so a case recorded on one machine
replays on another. The data tables (``ASCDATA.CFG`` and the links) are left
out: a replay folder's tables are stand-ins.

It needs only the standard library, so a program runs it with ``python -I``.
The tests use it through ``tests/conftest.py`` (``--hysplit-replay``,
``--hysplit-record``); the docs build and ``just`` through the command line::

    python tests/fixtures/replay.py folder DEST --cases tests/recorded/hysplit/5.4.2
    python tests/fixtures/replay.py folder DEST --cases DIR --record BUILD
    python tests/fixtures/replay.py folder DEST --cases DIR --python python3
    python tests/fixtures/replay.py summary DIR --record BUILD --met MET_DIR
    python tests/fixtures/replay.py builds [VERSION ...]

The builds are listed in ``hysplit-builds.toml`` at the repository's top
(not committed; ``RUNTRAJ_HYSPLIT_BUILDS`` names another file): see
:data:`BUILDS_EXAMPLE`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import lzma
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: The HYSPLIT programs runtraj runs.
PROGRAMS = ("hycs_std", "hyts_std")

#: The data tables runtraj takes from a HYSPLIT folder's ``bdyfiles/``.
DATA_TABLES = ("ASCDATA.CFG", "LANDUSE.ASC", "ROUGLEN.ASC", "TERRAIN.ASC")

#: The file runtraj sends a program's output to (``runtraj.models.hysplit.driver.LOG_FILE``).
LOG_FILE = "hysplit.log"

#: Files in a workdir that are not inputs of a case: the log the program
#: prints to, and ``ASCDATA.CFG``, which comes from the folder's tables.
NOT_INPUTS = frozenset({LOG_FILE, "ASCDATA.CFG"})

#: Files HYSPLIT writes that runtraj never reads, left out of a case to keep
#: the recordings small: the particle dump (``PARTICLE.DAT``, the largest),
#: the memory report, and copies of settings.
NOT_RECORDED = frozenset({"PARTICLE.DAT", "VMSDIST", "CONC.CFG", "STARTUP"})

#: The exit status of a replayed program whose inputs have no case.
MISS_STATUS = 97

#: What a met folder in ``CONTROL`` is written as when inputs are compared.
MET_FOLDER = "<met>/"

#: The folder of the recorded cases in this repository.
RECORDED = Path(__file__).resolve().parents[1] / "recorded" / "hysplit"

#: The file that says where this machine's HYSPLIT builds are.
BUILDS = Path(__file__).resolve().parents[2] / "hysplit-builds.toml"

#: What :data:`BUILDS` holds: a HYSPLIT folder (``exec/``, ``bdyfiles/``) per version.
BUILDS_EXAMPLE = """\
# Where this machine's HYSPLIT builds are: a folder with exec/ and bdyfiles/
# for each version that has recorded runs (tests/recorded/hysplit/).
[builds]
"5.0.2" = "/path/to/hysplit-5.0.2"
"5.4.2" = "/path/to/hysplit.v5.4.2_RHEL8.10_public"
"""


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


#: The start of an absolute path: ``/`` or, on Windows, a drive (``C:/``).
_ABSOLUTE = re.compile(r"/|[A-Za-z]:/")


def normalize(name: str, data: bytes) -> bytes:
    """Return input file *name* as it is compared: in ``CONTROL``, each met folder is ``<met>/``."""
    if name != "CONTROL":
        return data
    lines = data.decode().split("\n")
    lines = [
        MET_FOLDER if _ABSOLUTE.match(line) and line.rstrip().endswith("/") else line
        for line in lines
    ]
    return "\n".join(lines).encode()


def read_inputs(workdir: Path) -> dict[str, bytes]:
    """Return the input files of a call in *workdir*, normalized (:func:`normalize`)."""
    inputs = {}
    for path in sorted(workdir.iterdir()):
        if path.name in NOT_INPUTS or path.is_symlink() or not path.is_file():
            continue
        inputs[path.name] = normalize(path.name, path.read_bytes())
    return inputs


def case_key(inputs: dict[str, bytes]) -> str:
    """Return the key of a case: a hash of its input files' names and contents."""
    digest = hashlib.sha256()
    for name in sorted(inputs):
        data = inputs[name]
        digest.update(f"{name}\0{len(data)}\0".encode())
        digest.update(data)
    return digest.hexdigest()[:16]


def _snapshot(workdir: Path) -> dict[str, tuple[int, int]]:
    """Return the size and modification time of each regular file in *workdir*."""
    return {
        p.name: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in workdir.iterdir()
        if p.is_file() and not p.is_symlink()
    }


def _compress(data: bytes) -> bytes:
    """Return *data* compressed with xz: HYSPLIT's text output shrinks about 8 times."""
    return lzma.compress(data, preset=9 | lzma.PRESET_EXTREME)


def save_case(
    cases: Path,
    program: str,
    inputs: dict[str, bytes],
    outputs: dict[str, bytes],
    printed: bytes,
    status: int,
) -> Path:
    """Save one call under *cases*; a case with the same key is kept as it is."""
    folder = cases / program / case_key(inputs)
    if folder.exists():
        return folder
    folder.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{folder.name}-", dir=folder.parent))
    (tmp / "inputs").mkdir()
    for name, data in inputs.items():
        (tmp / "inputs" / name).write_bytes(data)
    (tmp / "outputs").mkdir()
    for name, data in outputs.items():
        (tmp / "outputs" / f"{name}.xz").write_bytes(_compress(data))
    (tmp / "printed.xz").write_bytes(_compress(printed))
    (tmp / "status.json").write_text(
        json.dumps({"status": status, "outputs": sorted(outputs)}, indent=1) + "\n"
    )
    try:
        tmp.rename(folder)  # another process may have saved the same case first
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
    return folder


# ---------------------------------------------------------------------------
# What a program of the folder does
# ---------------------------------------------------------------------------


def record(program: str, real: Path, cases: Path) -> int:
    """Run the real *program* in this folder, save the case, and return its exit status."""
    workdir = Path.cwd()
    inputs = read_inputs(workdir)
    before = _snapshot(workdir)
    done = subprocess.run(
        [str(real)], cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    sys.stdout.buffer.write(done.stdout)
    sys.stdout.flush()
    outputs = {
        name: (workdir / name).read_bytes()
        for name, stamp in _snapshot(workdir).items()
        if name not in NOT_INPUTS | NOT_RECORDED and before.get(name) != stamp
    }
    save_case(cases, program, inputs, outputs, done.stdout, done.returncode)
    if done.returncode < 0 and os.name == "posix":  # a signal: end the same way
        os.kill(os.getpid(), -done.returncode)
    return done.returncode


def replay(program: str, cases: Path, misses: Path) -> int:
    """Write the recorded output of the case with this folder's inputs, and return its status."""
    workdir = Path.cwd()
    inputs = read_inputs(workdir)
    key = case_key(inputs)
    folder = cases / program / key
    if not folder.is_dir():
        kept = misses / program / key
        if not kept.exists():
            kept.mkdir(parents=True)
            for name, data in inputs.items():
                (kept / name).write_bytes(data)
        print(
            f"replay: no recorded {program} run has these inputs (case {key}, "
            f"in {cases}). They are kept in {kept}. If runtraj now writes "
            "them on purpose, record the cases again: 'just record' "
            "(CONTRIBUTING.md, 'Testing against real HYSPLIT').",
            flush=True,
        )
        return MISS_STATUS
    meta = json.loads((folder / "status.json").read_text())
    for name in meta["outputs"]:
        (workdir / name).write_bytes(
            lzma.decompress((folder / "outputs" / f"{name}.xz").read_bytes())
        )
    sys.stdout.buffer.write(lzma.decompress((folder / "printed.xz").read_bytes()))
    sys.stdout.flush()
    status = meta["status"]
    if status < 0 and os.name == "posix":  # a signal: end the same way
        os.kill(os.getpid(), -status)
    return status


# ---------------------------------------------------------------------------
# The folder
# ---------------------------------------------------------------------------


def make_folder(
    dest: Path,
    cases: Path,
    real: Path | None = None,
    misses: Path | None = None,
    python: str | None = None,
) -> Path:
    """
    Make a HYSPLIT folder at *dest* that replays *cases*, or records them from the build *real*.

    *real* is a HYSPLIT folder (``exec/`` and ``bdyfiles/``). Recording, the
    folder has the programs *real* has, and its tables. Replaying, it has the
    programs *cases* holds cases of, and stand-in tables, and keeps the
    inputs of a miss in *misses* (``dest/misses`` unless given). The programs
    run ``replay.py`` with the interpreter that made the folder, or *python*
    as given (``python3``: found on ``PATH`` when a program runs). Returns
    the folder's ``exec/``, to put first on ``PATH``.
    """
    dest = Path(dest)
    cases = Path(cases).resolve()
    (dest / "exec").mkdir(parents=True, exist_ok=True)
    (dest / "bdyfiles").mkdir(exist_ok=True)
    if real is not None:
        real = Path(real).resolve()
        programs = [p for p in PROGRAMS if _program(real / "exec", p).is_file()]
        for table in DATA_TABLES:
            shutil.copyfile(real / "bdyfiles" / table, dest / "bdyfiles" / table)
    else:
        programs = [p for p in PROGRAMS if (cases / p).is_dir()]
        for table in DATA_TABLES:
            lines = [f"replayed HYSPLIT: a stand-in for {table}"] * 6
            (dest / "bdyfiles" / table).write_text("\n".join(lines) + "\n")
    this = Path(__file__).resolve()
    for program in programs:
        if real is not None:
            args = [
                "record",
                program,
                str(cases),
                str(_program(real / "exec", program)),
            ]
        else:
            kept = Path(misses) if misses is not None else dest / "misses"
            args = ["replay", program, str(cases), str(kept.resolve())]
        command = [python or sys.executable, "-I", str(this), "run", *args]
        if os.name == "nt":
            # Windows runs a .cmd as a program, and runtraj finds it as it finds
            # a .exe (PATHEXT); the last command's status is the script's.
            cmd = dest / "exec" / f"{program}.cmd"
            cmd.write_text(
                "@echo off\r\n"
                "rem A recorded HYSPLIT program: tests/fixtures/replay.py.\r\n"
                f"{subprocess.list2cmdline(command)}\r\n",
                newline="",
            )
            continue
        script = dest / "exec" / program
        script.write_text(
            "#!/bin/sh\n"
            "# A recorded HYSPLIT program: tests/fixtures/replay.py.\n"
            f"exec {shlex.join(command)}\n"
        )
        script.chmod(0o755)
    return dest / "exec"


def _program(folder: Path, name: str) -> Path:
    """Return the file of the HYSPLIT program *name* in *folder*: ``name``, or ``name.exe`` on Windows."""
    exe = folder / f"{name}.exe"
    return exe if os.name == "nt" and exe.is_file() else folder / name


def misses(folder: Path) -> list[Path]:
    """Return the inputs kept in *folder*, one folder per miss (``<program>/<key>``)."""
    folder = Path(folder)
    return sorted(folder.glob("*/*")) if folder.is_dir() else []


# ---------------------------------------------------------------------------
# recorded.yaml
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _printed_version(program: Path) -> str:
    """Return what *program* prints as its version, run where there is no ``CONTROL``."""
    with tempfile.TemporaryDirectory() as tmp:
        done = subprocess.run(
            [str(program)], cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
    found = re.search(r"HYSPLIT version:\s*(\S+)", done.stdout.decode(errors="replace"))
    return found.group(1) if found else ""


def summary(cases: Path, real: Path, met: Path) -> str:
    """Return ``recorded.yaml`` for the cases at *cases*, recorded from the build *real* on the met at *met*."""
    real = Path(real).resolve()
    names = set()
    for control in sorted(Path(cases).glob("*/*/inputs/CONTROL")):
        lines = control.read_text().split("\n")
        names.update(
            lines[i + 1].strip() for i, line in enumerate(lines) if line == MET_FOLDER
        )
    out = [
        "# The HYSPLIT build and met files these cases were recorded with",
        "# (tests/fixtures/replay.py; CONTRIBUTING.md, 'Testing against real HYSPLIT').",
        f"recorded: {dt.date.today().isoformat()}",
        "programs:",
    ]
    for program in PROGRAMS:
        path = real / "exec" / program
        if (Path(cases) / program).is_dir():
            out += [
                f"  {program}:",
                f"    version: {_printed_version(path)}",
                f"    sha256: {_sha256(path)}",
                f"    cases: {len(list((Path(cases) / program).iterdir()))}",
            ]
    out.append("met_files:")
    for name in sorted(names):
        path = Path(met) / name
        sha = _sha256(path) if path.is_file() else "missing"
        out.append(f"  {name}: {sha}")
    return "\n".join(out) + "\n"


def builds(versions: list[str]) -> list[tuple[str, Path]]:
    """Return the HYSPLIT folder of each version in :data:`BUILDS`, or of *versions* only."""
    import tomllib

    path = Path(os.environ.get("RUNTRAJ_HYSPLIT_BUILDS", BUILDS))
    if not path.is_file():
        raise SystemExit(f"No {path}. Write one like this:\n\n{BUILDS_EXAMPLE}")
    listed = tomllib.loads(path.read_text(encoding="utf-8")).get("builds", {})
    found = []
    for version in versions or list(listed):
        if version not in listed:
            raise SystemExit(f"{path} lists no build of HYSPLIT {version}.")
        folder = Path(listed[version]).expanduser()
        if not (folder / "exec" / "hycs_std").is_file():
            raise SystemExit(
                f"{folder} has no exec/hycs_std (HYSPLIT {version}, {path})."
            )
        found.append((version, folder))
    return found


def main(argv: list[str] | None = None) -> int:
    """Run one command: ``run`` (a program of a folder), ``folder`` or ``summary``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser(
        "run", help="run one program of a folder (its scripts call this)"
    )
    run.add_argument("mode", choices=["record", "replay"])
    run.add_argument("program", choices=PROGRAMS)
    run.add_argument("cases", type=Path)
    run.add_argument("other", type=Path, help="the real program, or the misses folder")
    folder = sub.add_parser("folder", help="make a HYSPLIT folder; prints its exec/")
    folder.add_argument("dest", type=Path)
    folder.add_argument("--cases", type=Path, required=True)
    folder.add_argument(
        "--record", type=Path, metavar="BUILD", help="record from this HYSPLIT folder"
    )
    folder.add_argument(
        "--python",
        help="the interpreter the programs run, as they name it (default: this one, "
        "by its path); 'python3' makes a folder that is the same wherever it is made",
    )
    summ = sub.add_parser("summary", help="write recorded.yaml in the cases folder")
    summ.add_argument("cases", type=Path)
    summ.add_argument("--record", type=Path, metavar="BUILD", required=True)
    summ.add_argument("--met", type=Path, required=True)
    listed = sub.add_parser(
        "builds", help="print each build in hysplit-builds.toml: VERSION PATH"
    )
    listed.add_argument("versions", nargs="*")
    args = parser.parse_args(argv)

    if args.command == "run":
        if args.mode == "record":
            return record(args.program, args.other, args.cases)
        return replay(args.program, args.cases, args.other)
    if args.command == "builds":
        for version, folder in builds(args.versions):
            print(version, folder)
        return 0
    if args.command == "folder":
        print(make_folder(args.dest, args.cases, args.record, python=args.python))
        return 0
    text = summary(args.cases, args.record, args.met)
    (args.cases / "recorded.yaml").write_text(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())

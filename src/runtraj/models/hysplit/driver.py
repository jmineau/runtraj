"""
Write HYSPLIT's input files, run ``hycs_std`` or ``hyts_std``, and read the particles it writes.

:func:`write_inputs` fills a working directory with everything
the program reads, and :func:`read_particle_dat` reads the particle file
it writes (:func:`runtraj.models.hysplit.read_cdump` the concentration
file of a concentration run, and :func:`runtraj.models.hysplit.read_tdump`
the trajectory file of a trajectory run). :class:`~runtraj.models.hysplit.HysplitModel` runs the two
around ``hycs_std`` in one directory; call them yourself to look at the
input files of a run, or to read a particle file from elsewhere.
"""

import datetime as dt
import logging
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from runtraj.exceptions import HYSPLITNotFoundError, SimulationError
from runtraj.models.hysplit.config import (
    CONCENTRATION_ONLY_SETTINGS,
    FIXED_SETUP,
    FIXED_TRAJECTORY_SETUP,
    PARTICLES_SETTINGS,
    TRAJECTORY_ONLY_SETTINGS,
    TRAJECTORY_SETUP_SETTINGS,
    WIND_ERROR_SETTINGS,
    ZI_ERROR_SETTINGS,
    HysplitConfig,
)
from runtraj.models.hysplit.control import ControlFile
from runtraj.models.hysplit.failures import (
    PARTICLE_LIMIT_PHRASE,
    FailureReason,
    failure_in,
)
from runtraj.models.hysplit.namelist import NameList

if TYPE_CHECKING:
    from arlmet import Grid

logger = logging.getLogger(__name__)

CONTROL_FILE = "CONTROL"
SETUP_FILE = "SETUP.CFG"
HYCS_STD_FILE = "hycs_std"
#: HYSPLIT's trajectory program, which the trajectory mode runs.
HYTS_STD_FILE = "hyts_std"
#: HYSPLIT's folders, as its download unpacks: the programs and the data tables.
EXEC_DIR = "exec"
BDYFILES_DIR = "bdyfiles"
#: HYSPLIT's data tables, which a run links into its workdir.
DATA_TABLES = ("ASCDATA.CFG", "LANDUSE.ASC", "ROUGLEN.ASC", "TERRAIN.ASC")
#: The name HYSPLIT gives the particle file it writes with ``ICHEM=8``.
PARTICLE_FILE = "PARTICLE_STILT.DAT"
WINDERR_FILE = "WINDERR"
ZIERR_FILE = "ZIERR"
ZICONTROL_FILE = "ZICONTROL"
#: The file ``hycs_std`` reads the conversions of ``ICHEM=2`` from.
CHEMRATE_FILE = "CHEMRATE.TXT"
LOG_FILE = "hysplit.log"
#: HYSPLIT's own file of warnings, such as the met running out.
WARNING_FILE = "WARNING"
#: HYSPLIT's own file of progress and error messages.
MESSAGE_FILE = "MESSAGE"
#: How many lines of ``MESSAGE`` a run that ended badly adds to its log.
MESSAGE_TAIL_LINES = 20

#: The settings ``CONTROL`` takes; :class:`ControlFile` writes them.
CONTROL_SETTINGS = ("n_hours", "emisshrs", "w_option", "z_top")

#: Settings runtraj uses itself, in no HYSPLIT input file.
RUNTRAJ_SETTINGS = ("directory", "mode")

#: Every setting that is not a ``SETUP.CFG`` entry. All other settings are.
NOT_IN_SETUP = frozenset(
    {
        *CONTROL_SETTINGS,
        "ziscale",  # ZICONTROL
        *WIND_ERROR_SETTINGS,
        *ZI_ERROR_SETTINGS,
        *RUNTRAJ_SETTINGS,
        *CONCENTRATION_ONLY_SETTINGS,  # CONTROL and CHEMRATE.TXT
        *TRAJECTORY_ONLY_SETTINGS,  # hyts_std's
    }
)


def program_name(config: HysplitConfig) -> str:
    """
    Return the HYSPLIT program a run with *config* is: ``hyts_std`` in the trajectory mode, else ``hycs_std``.

    Examples
    --------
    >>> program_name(HysplitConfig(n_hours=-24, mode="trajectory"))
    'hyts_std'
    """
    return HYTS_STD_FILE if config.mode == "trajectory" else HYCS_STD_FILE


def program_file(folder: Path, name: str) -> Path:
    """
    Return the file in *folder* that is the HYSPLIT program *name*: ``folder/name``, or on Windows the first of ``PATHEXT`` that exists.

    HYSPLIT's Windows download names its programs ``hycs_std.exe``. Without
    one in *folder*, the path of ``folder/name`` is returned, so a message
    can name it.

    Examples
    --------
    >>> program_file(Path("exec"), "hycs_std").stem
    'hycs_std'
    """
    path = folder / name
    if sys.platform == "win32":
        extensions = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
        for extension in extensions:
            if extension and (found := folder / f"{name}{extension.lower()}").is_file():
                return found
    return path


def find_program(name: str, directory: str | Path | None = None) -> Path:
    """
    Return the HYSPLIT program called *name* that a run uses: ``exec/<name>`` under *directory*, else the first on ``PATH``.

    runtraj does not include HYSPLIT, so the build is yours. *directory* is
    the HYSPLIT folder as unpacked (``directory`` under ``models: hysplit:``
    in ``config.yaml``), which holds ``exec/`` and ``bdyfiles/``.

    Parameters
    ----------
    name : str
        The program: ``hycs_std``, or ``hyts_std`` for a trajectory run.
    directory : str or Path, optional
        The HYSPLIT folder.

    Raises
    ------
    HYSPLITNotFoundError
        If *directory* has no ``exec/<name>``, or it is not executable;
        without *directory*, if no such program is on ``PATH``.
    """
    if directory is not None:
        exe = program_file(Path(directory) / EXEC_DIR, name)
        if not exe.is_file():
            raise HYSPLITNotFoundError(
                f"No {EXEC_DIR}/{exe.name} in {directory}. Set directory "
                "(under models: hysplit: in config.yaml) to the HYSPLIT folder "
                f"as unpacked, the one that holds {EXEC_DIR}/ and {BDYFILES_DIR}/."
            )
        if not os.access(exe, os.X_OK):
            raise HYSPLITNotFoundError(
                f"{exe} is not executable, so it cannot run. A HYSPLIT folder "
                "unpacked on Windows, or copied from there, loses that permission; "
                f"make it executable with: chmod +x {exe}"
            )
        return exe
    found = shutil.which(name)
    if found is None:
        raise HYSPLITNotFoundError(
            f"No {name} on PATH. runtraj does not include HYSPLIT: get "
            "it from NOAA ARL (https://www.ready.noaa.gov/HYSPLIT.php), then set directory (under "
            "models: hysplit: in config.yaml) to the HYSPLIT folder, or put the "
            f"folder that holds an executable {name} on PATH."
        )
    return Path(found)


def find_hycs_std(directory: str | Path | None = None) -> Path:
    """
    Return the ``hycs_std`` a run uses: ``exec/hycs_std`` under *directory*, else the first on ``PATH``.

    runtraj does not include HYSPLIT, so the build is yours. *directory* is
    the HYSPLIT folder as unpacked (``directory`` under ``models: hysplit:``
    in ``config.yaml``), which holds ``exec/`` and ``bdyfiles/``.

    Raises
    ------
    HYSPLITNotFoundError
        If *directory* has no ``exec/hycs_std``, or its ``hycs_std`` is not
        executable; without *directory*, if no ``hycs_std`` is on ``PATH``.
    """
    return find_program(HYCS_STD_FILE, directory)


def data_tables(
    directory: str | Path | None = None, program: str = HYCS_STD_FILE
) -> dict[str, Path | bytes]:
    """
    Return HYSPLIT's data tables a run uses, by name: a file to link, or the text to write.

    They come from ``bdyfiles/`` under *directory*, or, without
    *directory*, from a ``bdyfiles/`` folder beside the folder of the
    ``hycs_std`` on ``PATH`` (or of *program*, such as ``hyts_std`` for a
    trajectory run), as HYSPLIT's download lays them out. runtraj
    includes none. HYSPLIT's own ``ASCDATA.CFG`` says the tables are in
    ``../bdyfiles/``; a run links them into its workdir, so its
    ``ASCDATA.CFG`` is returned as text with that line, the sixth, set to
    ``'./'`` and the rest kept.

    Raises
    ------
    HYSPLITNotFoundError
        If the folder is missing or lacks one of the four tables, or,
        without *directory*, no *program* is on ``PATH``.
    """
    if directory is not None:
        folder = Path(directory) / BDYFILES_DIR
    else:
        folder = find_program(program).resolve().parent.parent / BDYFILES_DIR
    missing = [name for name in DATA_TABLES if not (folder / name).is_file()]
    if missing:
        raise HYSPLITNotFoundError(
            f"No {', '.join(missing)} in {folder}. runtraj includes no HYSPLIT "
            f"data tables: they are in the {BDYFILES_DIR}/ folder of HYSPLIT's "
            f"download, beside its {EXEC_DIR}/. Set directory (under models: "
            "hysplit: in config.yaml) to a HYSPLIT folder that has both."
        )
    tables: dict[str, Path | bytes] = {name: folder / name for name in DATA_TABLES}
    tables["ASCDATA.CFG"] = _tables_here(folder / "ASCDATA.CFG")
    return tables


#: The sixth line of ``ASCDATA.CFG``, where HYSPLIT reads the tables: the
#: workdir, where a run links them.
_TABLES_HERE = b"'./'           directory location of data files\n"


def _tables_here(path: Path) -> bytes:
    """Return the ``ASCDATA.CFG`` at *path* with its sixth line, where the tables are, as ``'./'``."""
    lines = path.read_bytes().splitlines(keepends=True)
    if len(lines) >= 6:
        lines[5] = _TABLES_HERE
    return b"".join(lines)


#: HYSPLIT's names for the columns the particle table names otherwise
#: (:data:`runtraj.particles.PARTICLE_SCHEMA`); the rest keep HYSPLIT's.
PARTICLE_COLUMNS = {"indx": "particle", "time": "age", "long": "lon", "lati": "lat"}

# Before pandas 3 the C reader's default float parser can differ from Python's
# in the last digit, and ``round_trip`` reads every value exactly; pandas 3
# parses exactly by default and deprecates the argument.
_EXACT_FLOATS: dict[str, Any] = (
    {"float_precision": "round_trip"} if int(pd.__version__.split(".")[0]) < 3 else {}
)


def read_particle_dat(path: str | Path, columns: Sequence[str]) -> pd.DataFrame:
    """
    Read HYSPLIT's particle file as a particle table.

    The ``varsiwant`` codes ``indx``, ``time``, ``long``, and ``lati``
    become the particle table's ``particle``, ``age``, ``lon``, and ``lat``
    (:data:`PARTICLE_COLUMNS`).

    Parameters
    ----------
    path : str or Path
        The file HYSPLIT wrote.
    columns : sequence of str
        Its columns, the ``varsiwant`` of the run, in order.

    Returns
    -------
    pandas.DataFrame
        One row per particle per output step, as HYSPLIT wrote them. The
        release heights are added after the run
        (:func:`runtraj.models.run_model`).

    Raises
    ------
    ValueError
        If the file has a different number of columns.
    """
    path = Path(path)
    names = [PARTICLE_COLUMNS.get(name, name) for name in columns]
    # pandas' C reader splits HYSPLIT's variable-width spacing and reads a
    # day's file three times faster than numpy.loadtxt, with the same values.
    # The first line is the release location, not a header.
    try:
        particles = pd.read_csv(
            path,
            sep=r"\s+",
            skiprows=1,
            header=None,
            dtype=np.float64,
            **_EXACT_FLOATS,
        )
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=pd.Index(names))

    if particles.shape[1] != len(names):
        raise ValueError(
            f"{path.name} has {particles.shape[1]} columns, expected {len(names)} "
            f"from varsiwant={list(columns)!r}."
        )
    particles.columns = pd.Index(names)
    return particles


# -- what each input file holds -----------------------------------------------


def setup_entries(params: HysplitConfig) -> dict[str, Any]:
    """
    Return the ``SETUP.CFG`` namelist entries of *params*, leaving out unset ones.

    In the particles mode the entries of :data:`FIXED_SETUP` come first.
    Then every setting not in :data:`NOT_IN_SETUP`, in declaration order,
    under their own names; the concentration mode leaves out the particle
    file's settings (:data:`PARTICLES_SETTINGS`), and its ``ICHEM`` is the
    config's ``ichem``, 0 unless set. The driver adds
    ``KMSL``, ``IVMAX`` (in the particles mode), and ``WINDERRTF``, and ``MAXPAR`` when ``maxpar``
    is unset, which depend on the release locations or follow from other
    settings.

    A trajectory run gets :data:`FIXED_TRAJECTORY_SETUP` and the settings
    of :data:`TRAJECTORY_SETUP_SETTINGS` only: ``hyts_std`` reads the
    dispersion model's entries too (``ICHEM`` and ``IDSP`` change its
    trajectories), so none of them is written. The driver adds ``KMSL``.
    """
    if params.mode == "trajectory":
        entries = dict(FIXED_TRAJECTORY_SETUP)
        entries.update(
            (name, getattr(params, name))
            for name in TRAJECTORY_SETUP_SETTINGS
            if getattr(params, name) is not None
        )
        return entries
    particles = params.mode == "particles"
    entries = dict(FIXED_SETUP) if particles else {}
    skip = NOT_IN_SETUP if particles else NOT_IN_SETUP | set(PARTICLES_SETTINGS)
    entries.update(
        (name, getattr(params, name))
        for name in HysplitConfig.model_fields
        if name not in skip and getattr(params, name) is not None
    )
    entries["zicontroltf"] = zicontroltf(params)
    if params.seed is not None:
        entries["seed"] = setup_seed(params.seed)
    return entries


def setup_seed(seed: int) -> int:
    """
    Return the ``SEED`` value written to ``SETUP.CFG`` for a *seed* of 0 or more.

    HYSPLIT sets its generator state to ``-1 + SEED``. Under ``krand=2`` it
    reinitializes only from a negative state, and every state of -1 or more
    gives the same stream. Writing ``-(seed + 1)`` puts the state at
    ``-(seed + 2)``. That is negative, different for each seed, and never
    the unseeded default (``SEED = 0``).
    :class:`~runtraj.models.hysplit.HysplitConfig` refuses a negative seed,
    which this mapping would give the same ``SEED`` as its positive.

    This mapping is for HYSPLIT before 5.5, which ignores a positive
    ``SEED``. It goes when 5.5 is the minimum version.

    Examples
    --------
    >>> setup_seed(0)
    -1
    >>> setup_seed(42)
    -43
    """
    return -(seed + 1)


def release_locations(locations: Sequence[tuple[float, float, float]]) -> int:
    """
    Return the number of horizontal locations among the release *locations*.

    HYSPLIT divides ``numpar`` among them. Two release locations at one
    latitude and longitude are one: HYSPLIT joins their heights into one
    line source.
    """
    return len({(lon, lat) for lon, lat, _ in locations})


def check_release(
    workdir: Path,
    locations: Sequence[tuple[float, float, float]],
    config: HysplitConfig,
) -> None:
    """
    Raise when HYSPLIT stopped releasing before every one of *locations* got its particles.

    HYSPLIT fills the locations in order and stops at ``MAXPAR``, writing
    ``exceeding puff limit`` to its ``WARNING`` file, so the last
    locations get fewer particles or none. From one location that is
    harmless (:data:`~runtraj.models.hysplit.failures.PARTICLE_LIMIT_PHRASE`).

    Raises
    ------
    SimulationError
        With ``reason`` ``PARTICLE_LIMIT``, when *locations* hold several
        horizontal locations and the ``WARNING`` file in *workdir* says
        HYSPLIT reached ``MAXPAR``.
    """
    count = release_locations(locations)
    warning = workdir / WARNING_FILE
    if count < 2 or not warning.exists():
        return
    if PARTICLE_LIMIT_PHRASE in warning.read_text(encoding="utf-8", errors="replace"):
        fix = (
            "Raise maxpar or leave it unset." if config.maxpar else "Set maxpar higher."
        )
        raise SimulationError(
            f"HYSPLIT stopped releasing at maxpar = {config.maxpar_for(count)} "
            f"before each of the {count} release locations got its share of "
            f"numpar = {config.numpar}. {fix}",
            reason=FailureReason.PARTICLE_LIMIT,
        )


#: How far inside the met's domain, in grid cells, a particle at the last time
#: step of a run that stopped early must be for :func:`check_met_stop` to fail
#: the run. Measured on HRRR (3 km), NAM (12 km) and NARR (32 km) grids and
#: crops of them: particles that left the domain ended 1 to 2 cells from its
#: edge, and in runs the met stopped on a domain a hundred cells across or
#: more, the particle furthest inside was 34 cells in or more.
MET_STOP_CELLS = 5


def cells_from_edge(lon: Any, lat: Any, grid: "Grid") -> np.ndarray:
    """
    Return how far each position is from the nearest side of the met grid, in grid cells.

    A global grid's columns wrap around in longitude, so it has no east or
    west side. A position outside the grid has a negative distance. The
    domain top is not an edge: HYSPLIT keeps particles from rising above it.

    Parameters
    ----------
    lon, lat : array-like
        Positions, in degrees.
    grid : arlmet.Grid
        The met's horizontal grid.

    Returns
    -------
    numpy.ndarray
        The distance of each position, in grid cells.
    """
    x, y = grid.fractional_indices(np.asarray(lon), np.asarray(lat))
    edges = [y, grid.ny - 1 - y]
    if not grid.wraps_lon:
        edges += [x, grid.nx - 1 - x]
    return np.minimum.reduce(edges)


def check_met_stop(particles: pd.DataFrame, n_hours: int, grid: "Grid") -> None:
    """
    Raise when a run stopped before ``n_hours`` with particles well inside the met's domain.

    Particles that leave the met's domain end at its edge, and a run whose
    particles all left early is complete. When the met runs out, HYSPLIT
    can stop every particle where it is, write no warning, and report a
    complete run. So a run that stopped before ``n_hours`` fails when any
    particle at its last time step is more than :data:`MET_STOP_CELLS`
    grid cells from every side of the met grid (:func:`cells_from_edge`).

    On a domain only a few dozen cells across, a met stop late in a run,
    when nearly every particle has already left, can pass as a domain exit.
    Little of such a run is lost.

    The particle table must hold a row for every time step (``outdt: 0``).
    With fewer rows, the last row of a particle that left can be far
    inside the domain. A table without the ``particle``, ``age``, ``lon``,
    and ``lat`` columns is not checked.

    Parameters
    ----------
    particles : pandas.DataFrame
        The particles of one run.
    n_hours : int
        The length of the run asked for, negative for a backward run.
    grid : arlmet.Grid
        The horizontal grid of the met the run read.

    Raises
    ------
    SimulationError
        With ``reason`` ``MET_COVERAGE``, when the particles stopped
        before ``n_hours`` and one at the last time step is more than
        :data:`MET_STOP_CELLS` grid cells inside the met grid.
    """
    if particles.empty or not {"particle", "age", "lon", "lat"} <= set(
        particles.columns
    ):
        return
    reach = particles["age"].abs()
    stop = reach.max()
    if stop >= abs(n_hours) * 60:
        return
    last = particles[reach == stop]
    inside = cells_from_edge(last["lon"], last["lat"], grid).max()
    if inside > MET_STOP_CELLS:
        share = len(last) / particles["particle"].nunique()
        raise SimulationError(
            f"The particles stopped {stop / 60:.1f} h into the {abs(n_hours)} h "
            f"run, up to {inside:.1f} grid cells inside the met's domain, not "
            f"at its edge: the met ran out ({share:.0%} of the particles were "
            "still in the air).",
            reason=FailureReason.MET_COVERAGE,
        )


def ziscale_factors(params: HysplitConfig) -> list[float] | None:
    """
    Return the hourly mixed-layer factors for ``ZICONTROL``, or ``None`` when unscaled.

    A single ``ziscale`` value is repeated for every hour of the run and a
    list is used as given. Factors that are all 1.0 give ``None``.
    """
    values = params.hourly_ziscale()
    if all(v == 1.0 for v in values):
        return None
    return values


def zicontroltf(params: HysplitConfig) -> int:
    """Return HYSPLIT's ``ZICONTROLTF`` flag: 1 when ``ziscale`` scales the mixed layer."""
    return int(ziscale_factors(params) is not None)


def chemrate(params: HysplitConfig) -> list[str] | None:
    """
    Return the lines of ``CHEMRATE.TXT`` for *params*, or ``None`` when it has no conversions.

    One line per conversion, as ``CHEM02`` reads them: the number of the
    pollutant converted and of the one it becomes, counted from 1 in the
    order of ``pollutants``, the fraction converted per hour, and the
    weight ratio. A run with ``ichem: 2`` always gets the file: without
    one HYSPLIT converts pollutant 1 to 2 at 0.10 per hour.
    """
    if not params.conversions:
        return None
    number = {p.name: k for k, p in enumerate(params.pollutants, start=1)}
    return [
        f"{number[c.source]} {number[c.product]} {c.rate!r} {c.weight_ratio!r}"
        for c in params.conversions
    ]


def winderr(params: HysplitConfig) -> list[float] | None:
    """Return the ``WINDERR`` values, in the file's order, or ``None`` when unset."""
    values = [getattr(params, name) for name in WIND_ERROR_SETTINGS]
    return None if values[0] is None else values


def zierr(params: HysplitConfig) -> list[float] | None:
    """Return the ``ZIERR`` values, in the file's order, or ``None`` when unset."""
    values = [getattr(params, name) for name in ZI_ERROR_SETTINGS]
    return None if values[0] is None else values


def winderrtf(params: HysplitConfig) -> int:
    """Return HYSPLIT's ``WINDERRTF`` flag: 1 for wind errors, 2 for mixed-layer errors, 3 for both."""
    return (winderr(params) is not None) + 2 * (zierr(params) is not None)


def _write_values(path: Path, values: list[float] | None) -> None:
    """Write one value per line to ``path``; nothing is written when ``values`` is ``None``."""
    if values is not None:
        path.write_text(
            "\n".join(str(v) for v in values) + "\n", encoding="utf-8", newline="\n"
        )


def write_inputs(
    workdir: str | Path,
    release_time: dt.datetime,
    locations: Sequence[tuple[float, float, float]],
    config: HysplitConfig,
    met_files: Sequence[Path],
    kmsl: int = 0,
) -> None:
    """
    Write everything ``hycs_std`` reads for one run into *workdir*, or ``hyts_std`` in the trajectory mode.

    Links the program (from ``config.directory``, or else ``PATH``:
    :func:`find_program`) and HYSPLIT's data tables
    (:func:`data_tables`), and writes ``CONTROL`` and ``SETUP.CFG``, plus
    ``ZICONTROL``, ``WINDERR``, and ``ZIERR`` when the settings call for
    them. *workdir* is created if needed, and should be empty.

    Parameters
    ----------
    workdir : str or Path
        Directory to write into.
    release_time : datetime
        The release time, in UTC.
    locations : sequence of tuple of float
        The release locations, each ``(longitude, latitude, height)`` in
        degrees and meters, in the order ``CONTROL`` lists them.
    config : HysplitConfig
        Transport and error settings.
    met_files : sequence of Path
        Meteorology files, in the order HYSPLIT should read them.
    kmsl : {0, 1}, default 0
        HYSPLIT's ``KMSL``: 0 when the release heights are above ground, 1
        when they are above sea level.

    Raises
    ------
    HYSPLITNotFoundError
        If there is no program to link (:func:`find_program`).
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    # Link the binary and the data tables; only hycs_std is
    # taken from exec/, which holds dozens of other programs.
    program = program_name(config)
    trajectory = config.mode == "trajectory"
    found = find_program(program, config.directory)
    # On Windows the link keeps the program's extension, which finds it again.
    linked = program + found.suffix.lower() if sys.platform == "win32" else program
    links = {linked: found}
    for name, table in data_tables(config.directory, program).items():
        if isinstance(table, bytes):
            (workdir / name).write_bytes(table)
        else:
            links[name] = table
    for name, target in links.items():
        _link(workdir / name, target.resolve())

    ControlFile(
        release_time=release_time,
        locations=list(locations),
        n_hours=config.n_hours,
        emisshrs=config.emisshrs,
        w_option=config.w_option,
        z_top=config.z_top,
        met_files=list(met_files),
        pollutants=config.pollutants,
        grids=config.grids,
        trajectory=trajectory,
    ).write(workdir / CONTROL_FILE)

    entries = setup_entries(config)
    entries["kmsl"] = kmsl
    if trajectory:  # none of the dispersion model's entries or files
        setup = NameList("SETUP")
        setup.update(entries)
        setup.write(workdir / SETUP_FILE)
        return
    entries["maxpar"] = config.maxpar_for(release_locations(locations))
    if config.mode == "particles":
        entries["ivmax"] = len(config.varsiwant)  # number of output variables
    entries["winderrtf"] = winderrtf(config)
    setup = NameList("SETUP")
    setup.update(entries)
    setup.write(workdir / SETUP_FILE)

    factors = ziscale_factors(config)
    if factors is not None:
        (workdir / ZICONTROL_FILE).write_text(
            "\n".join([str(len(factors)), *(str(v) for v in factors)]) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    rates = chemrate(config)
    if rates is not None:
        (workdir / CHEMRATE_FILE).write_text(
            "\n".join(rates) + "\n", newline="\n", encoding="utf-8"
        )
    _write_values(workdir / WINDERR_FILE, winderr(config))
    _write_values(workdir / ZIERR_FILE, zierr(config))


def _link(path: Path, target: Path) -> None:
    """
    Make *path* a symbolic link to *target*; on Windows, a hard link or a copy where it may not.

    Windows lets only an administrator, or a user in developer mode, create a
    symbolic link. A hard link needs neither and takes no space, but only to
    a file on the same drive (NTFS); a copy works anywhere. Each does what
    the link does.
    """
    try:
        path.symlink_to(target)
        return
    except OSError:
        if sys.platform != "win32":
            raise
    try:
        os.link(target, path)
    except OSError:
        shutil.copy2(target, path)


def run_hycs_std(
    workdir: Path, timeout: int | None = None, program: str = HYCS_STD_FILE
) -> None:
    """
    Run the ``hycs_std`` in *workdir*, writing its output to ``hysplit.log``.

    *workdir* holds the inputs :func:`write_inputs` wrote. *program* names
    another HYSPLIT program linked there, ``hyts_std`` for a trajectory run. With *timeout*
    seconds the run is stopped when it takes longer; without it, it may run
    as long as HYSPLIT does. If this process is interrupted while it waits
    (``KeyboardInterrupt`` or ``SystemExit``), the run is stopped too.

    Raises
    ------
    SimulationError
        With ``reason`` ``TIMEOUT`` when the run exceeded *timeout*
        seconds, the :class:`FailureReason` of a known failure message in
        the log or HYSPLIT's ``WARNING`` file, such as ``no more
        meteorology`` (``MET_COVERAGE``), or ``EXIT_STATUS`` when
        ``hycs_std`` ended with a non-zero exit status or was killed by a
        signal, such as the kernel's out-of-memory killer, and the log has
        no known message. Its log then ends with the last lines of
        HYSPLIT's ``MESSAGE`` file. Particles that all left the met's
        domain are not a failure.
    """
    log_path = workdir / LOG_FILE
    with (
        log_path.open("w", encoding="utf-8") as handle,
        subprocess.Popen(
            [str(program_file(workdir, program))],
            cwd=workdir,
            stdout=handle,
            stderr=subprocess.STDOUT,
            **_own_group(),
        ) as proc,
        _ended_if_this_process_dies(proc),
    ):
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            _terminate(proc)
            raise SimulationError(
                f"HYSPLIT ran longer than the {timeout} s timeout.",
                reason=FailureReason.TIMEOUT,
            ) from e
        except BaseException:
            # Ctrl-C, or a worker told to stop: HYSPLIT runs in a session of
            # its own (a process group on Windows), so no signal reached it and
            # it would run on, unseen.
            _terminate(proc)
            raise
    messages = log_path.read_text(encoding="utf-8", errors="replace")
    warnings = workdir / WARNING_FILE
    if warnings.exists():
        messages += warnings.read_text(encoding="utf-8", errors="replace")
    found = failure_in(messages)
    if found is not None:
        reason, line = found
        raise SimulationError(f"HYSPLIT: {line}", reason=reason)
    if proc.returncode != 0:
        raise _exit_status_error(workdir, proc.returncode)


def _exit_status_error(workdir: Path, status: int) -> SimulationError:
    """
    Return the error for a ``hycs_std`` that ended with *status* but no known message.

    *status* is ``subprocess``'s: negative for the signal that killed the
    process. The tail of HYSPLIT's ``MESSAGE`` file is added to the log,
    where it says how far the run got.
    """
    if status < 0:
        try:
            name = signal.Signals(-status).name
        except ValueError:  # a real-time signal has no name
            name = f"signal {-status}"
        how = f"was killed by {name}"
    else:
        how = f"exited with status {status}"
    message = f"HYSPLIT {how} before it finished."
    path = workdir / MESSAGE_FILE
    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-MESSAGE_TAIL_LINES:])
        with (workdir / LOG_FILE).open("a", encoding="utf-8") as log:
            log.write(f"\nruntraj: HYSPLIT {how}. The end of {MESSAGE_FILE}:\n{tail}\n")
    return SimulationError(message, reason=FailureReason.EXIT_STATUS)


def _own_group() -> dict[str, Any]:
    """Return the ``Popen`` arguments that start HYSPLIT in a session of its own (a new process group on Windows)."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


#: The watchdog of a HYSPLIT run on POSIX: it waits for its standard input
#: to close, which happens when the process that started it ends however it
#: ends, then kills the process group its argument names.
_WATCHDOG = 'read line; kill -s KILL -"$0" 2>/dev/null'


@contextmanager
def _ended_if_this_process_dies(proc: subprocess.Popen[Any]) -> Iterator[None]:
    """
    End HYSPLIT (*proc*) if this process dies while the block runs, even when it is killed outright.

    A worker the out-of-memory killer or ``kill -9`` ends runs no
    ``finally`` block, and HYSPLIT, in a session of its own, would run on.
    On POSIX a watchdog shell, in a session of its own too, reads a pipe from
    this process, which the system closes when this process dies; it then
    kills HYSPLIT's process group. On Windows HYSPLIT is put in a job object
    that ends it when the job's last handle, held here, closes. Either is in
    place a moment after HYSPLIT starts, so a process killed in that moment
    still leaves it running. Where neither can be set up, HYSPLIT runs
    without it.
    """
    if sys.platform == "win32":
        job = _job_that_ends(proc.pid)
        try:
            yield
        finally:
            if job is not None:
                _close_handle(job)  # HYSPLIT has ended, or is ended now
        return
    try:
        watchdog = subprocess.Popen(
            ["/bin/sh", "-c", _WATCHDOG, str(proc.pid)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # so Ctrl-C to this process leaves it
        )
    except OSError as e:
        logger.warning("HYSPLIT runs without a watchdog: %s", e)
        yield
        return
    try:
        yield
    finally:
        watchdog.kill()  # before its pipe closes, so it kills nothing
        watchdog.wait()
        if watchdog.stdin is not None:
            watchdog.stdin.close()


if sys.platform == "win32":  # Windows' job objects, through ctypes
    #: The limit that ends a job's processes when its last handle closes (``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``).
    _KILL_ON_JOB_CLOSE = 0x2000
    #: The class of ``SetInformationJobObject`` the limit is in (``JobObjectExtendedLimitInformation``).
    _EXTENDED_LIMITS = 9
    #: The access to a process that putting it in a job needs (``PROCESS_SET_QUOTA | PROCESS_TERMINATE``).
    _JOIN_ACCESS = 0x0101

    def _job_that_ends(pid: int) -> int | None:
        """
        Put the Windows process *pid* in a new job object that ends it when the job closes; return the job's handle.

        The job closes when its last handle does, and its one handle is held by
        this process, so the system ends *pid* when this process ends, however
        it ends. Returns ``None`` where the job cannot be made.
        """
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            logger.warning(
                "HYSPLIT runs outside a job object: %s",
                ctypes.WinError(ctypes.get_last_error()),
            )
            return None
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        process = kernel32.OpenProcess(_JOIN_ACCESS, False, pid)
        joined = (
            process
            and kernel32.SetInformationJobObject(
                job, _EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)
            )
            and kernel32.AssignProcessToJobObject(job, process)
        )
        error = ctypes.get_last_error()
        if process:
            kernel32.CloseHandle(process)
        if not joined:
            kernel32.CloseHandle(job)
            logger.warning(
                "HYSPLIT runs outside a job object: %s", ctypes.WinError(error)
            )
            return None
        return job

    def _close_handle(handle: int) -> None:
        """Close a Windows handle."""
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(handle)


def _terminate(proc: subprocess.Popen[Any]) -> None:
    """Stop a HYSPLIT process (its group, on POSIX) with SIGTERM, then SIGKILL if it does not exit."""
    try:
        _signal_group(proc, hard=False)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        _signal_group(proc, hard=True)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.wait()


def _signal_group(proc: subprocess.Popen[Any], *, hard: bool) -> None:
    """
    Send SIGKILL (*hard*) or SIGTERM to *proc*'s process group.

    Windows has no process group to signal: ``terminate`` and ``kill`` end
    the process alone, which is all HYSPLIT is (it starts no other).
    """
    if sys.platform == "win32":
        try:
            if hard:
                proc.kill()
            else:
                proc.terminate()
        except OSError as e:  # the process has already ended
            raise ProcessLookupError from e
        return
    os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)

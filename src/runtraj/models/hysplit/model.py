"""HYSPLIT as a transport model."""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import re
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

import xarray as xr

from runtraj.concentration import require_netcdf4
from runtraj.exceptions import SimulationError
from runtraj.models import ModelRun

from .cdump import read_cdump
from .config import ConcentrationGrid, HysplitConfig
from .control import cdump_name
from .driver import (
    LOG_FILE,
    PARTICLE_FILE,
    check_met_stop,
    check_release,
    data_tables,
    find_hycs_std,
    read_particle_dat,
    run_hycs_std,
    write_inputs,
)
from .failures import FailureReason
from .met import Met, MetConfig
from .releases import check_releases, release_rows

if TYPE_CHECKING:
    import pandas as pd
    from arlmet import Grid

    from runtraj.releases import Release


#: What ``hycs_std`` prints on its first lines: ``HYSPLIT version: hysplit.v5.1.0``.
_VERSION_LINE = re.compile(r"HYSPLIT version:\s*(\S+)")

#: Seconds ``hycs_std`` gets to print its version; it stops at once without ``CONTROL``.
_VERSION_TIMEOUT = 30


def hysplit_version(directory: str | Path | None = None) -> str | None:
    """
    Return the version the ``hycs_std`` of *directory*, or else on ``PATH``, prints when it starts.

    ``hycs_std`` prints a line such as ``HYSPLIT version: hysplit.v5.1.0``
    before it reads its input; this returns ``hysplit.v5.1.0``. It runs the
    binary for a moment in an empty temporary directory, where it stops for
    want of a ``CONTROL`` file. The version is recorded with a run's
    settings, but it is not what tells two builds apart: the SHA-256 of
    ``hycs_std`` is (:meth:`HysplitModel.sha256`). Two builds of one
    release print the same version, and CHPC's ``hysplit/5.2.3`` module
    prints ``hysplit.v5.2.2``.

    Returns
    -------
    str or None
        The version, or ``None`` when ``hycs_std`` printed none or could
        not be run here.

    Raises
    ------
    HYSPLITNotFoundError
        If there is no ``hycs_std`` (:func:`find_hycs_std`).
    """
    return _printed_version(*_stamp(find_hycs_std(directory)))


def _stamp(path: Path) -> tuple[str, int, int]:
    """Return *path*, resolved, with its modification time and size, the key of the caches below."""
    path = path.resolve()
    stat = path.stat()
    return str(path), stat.st_mtime_ns, stat.st_size


@functools.lru_cache(maxsize=16)
def _printed_version(path: str, mtime_ns: int, size: int) -> str | None:
    """Return the version the ``hycs_std`` at *path* prints, read once per file."""
    with tempfile.TemporaryDirectory(prefix="runtraj-version-") as tmp:
        try:
            proc = subprocess.Popen(
                [path],
                cwd=tmp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError:  # not executable here, such as another platform's build
            return None
        try:
            out, _ = proc.communicate(timeout=_VERSION_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return None
    found = _VERSION_LINE.search(out.decode(errors="replace"))
    return found.group(1) if found else None


@functools.lru_cache(maxsize=16)
def _file_sha256(path: str, mtime_ns: int, size: int) -> str:
    """Return the SHA-256 of the file at *path*, read once per file."""
    return _sha256(Path(path))


def _sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class HysplitModel:
    """
    HYSPLIT as a transport model, the object ``get_model("hysplit")`` returns.

    :meth:`run` picks the met files a release needs, writes HYSPLIT's input
    files (:func:`~runtraj.models.hysplit.write_inputs`), runs ``hycs_std``,
    and reads the particles it writes
    (:func:`~runtraj.models.hysplit.read_particle_dat`), or, in the
    concentration mode, the file of each grid
    (:func:`~runtraj.models.hysplit.read_cdump`). HYSPLIT reads the
    met where it is (the cropped copies when the met is cropped), so nothing
    is staged per run.
    """

    name = "hysplit"
    config_class = HysplitConfig
    #: HYSPLIT reads ARL files, found, downloaded, or cropped as a met says.
    met_config_class = MetConfig
    #: HYSPLIT writes its inputs and outputs as files in a directory.
    needs_workdir = True
    #: Its particle file, left out of a failed run's kept workdir.
    particle_files = (PARTICLE_FILE,)
    #: One hycs_std process per release.
    batched = False

    def result(self, config: HysplitConfig) -> str:
        """
        Return the type of result a run with *config* makes: its ``mode``, ``particles`` or ``concentration``.

        Examples
        --------
        >>> HysplitModel().result(HysplitConfig(n_hours=-24))
        'particles'
        """
        return config.mode

    def sha256(self, config: HysplitConfig) -> str:
        """
        Return the SHA-256 of the ``hycs_std`` *config* runs, which tells its build from any other.

        The ``hycs_std`` of ``config.directory``, or else on ``PATH``
        (:func:`find_hycs_std`). Read once per file.

        Raises
        ------
        HYSPLITNotFoundError
            If there is no ``hycs_std``.
        """
        return _file_sha256(*_stamp(find_hycs_std(config.directory)))

    def version(self, config: HysplitConfig) -> str | None:
        """Return the version the ``hycs_std`` *config* runs prints when it starts (:func:`hysplit_version`)."""
        return hysplit_version(config.directory)

    def data_files(self, config: HysplitConfig) -> dict[str, str]:
        """
        Return the SHA-256 of each data table a run uses, by name.

        The tables of :func:`~runtraj.models.hysplit.data_tables`, as the
        run gets them: ``ASCDATA.CFG`` with its line of where the tables are
        set to the workdir, so where a HYSPLIT folder keeps them changes
        nothing.

        Raises
        ------
        HYSPLITNotFoundError
            If the tables cannot be found.
        """
        return {
            name: hashlib.sha256(
                table if isinstance(table, bytes) else table.read_bytes()
            ).hexdigest()
            for name, table in sorted(data_tables(config.directory).items())
        }

    def check_met(
        self, met: MetConfig, releases: pd.DataFrame, n_hours: int
    ) -> pd.DataFrame:
        """
        Return what would stop HYSPLIT from running *releases* with *met*, found before any run.

        The checks of :meth:`Met.check_releases`: the met's directory
        holds ARL files, every file a run needs is there, and the releases
        are inside the crop. Nothing is run or downloaded, and files are read
        no further than their headers.

        Parameters
        ----------
        met : MetConfig
            The met, with an absolute directory in a project.
        releases : pandas.DataFrame
            One row per release point, with ``release``, ``time``,
            ``longitude``, and ``latitude`` columns.
        n_hours : int
            The length of the runs, which sets the files each needs.

        Returns
        -------
        pandas.DataFrame
            ``check``, ``release`` (``NA`` for a problem of the met
            itself), and ``message`` columns, one row for each problem.
        """
        return Met("met", met).check_releases(releases, n_hours)

    def fetch_met(
        self, met: MetConfig, windows: Iterable[tuple[dt.datetime, dt.datetime]]
    ) -> pd.DataFrame:
        """
        Get the met files that cover *windows*, downloading the ones a download lacks.

        The files :meth:`run` would ask for, one request to the archive for
        windows that overlap (:meth:`Met.fetch`). A met without ``download``
        is only checked for missing files.

        Parameters
        ----------
        met : MetConfig
            The met, with an absolute directory in a project.
        windows : iterable of tuple of datetime
            The time each run covers (:func:`runtraj.meteorology.run_window`).

        Returns
        -------
        pandas.DataFrame
            ``file`` and ``message`` columns: a row for each file, then a
            row for each window whose files could not be had.
        """
        return Met("met", met).fetch(windows)

    def downloads_met(self, met: MetConfig) -> bool:
        """
        Whether *met* downloads its files from an ARL archive (``download`` is set).

        Examples
        --------
        >>> met = MetConfig(directory="/data/hrrr", download="hrrr")
        >>> HysplitModel().downloads_met(met)
        True
        """
        return met.download is not None

    def check_releases(
        self, releases: pd.DataFrame, config: HysplitConfig
    ) -> pd.DataFrame:
        """
        Return the releases HYSPLIT cannot run: multipoint releases whose points share a location.

        No setting changes which, so *config* is not read. See
        :func:`runtraj.models.hysplit.releases.check_releases`.
        """
        return check_releases(releases)

    def release_rows(self, particles: pd.DataFrame, release: Release) -> pd.DataFrame:
        """
        Return where each particle started, for a HYSPLIT build that writes no row at ``age = 0``.

        See :func:`runtraj.models.hysplit.releases.release_rows`.
        """
        return release_rows(particles, release)

    def run(
        self,
        release: Release,
        config: HysplitConfig,
        met: MetConfig,
        window: tuple[dt.datetime, dt.datetime],
        workdir: Path | None = None,
        timeout: int | None = None,
    ) -> ModelRun:
        """
        Run HYSPLIT for one release in *workdir* and return its particles, or its grids in the concentration mode.

        It finds the met files that cover *window*, up to the met step at
        or after its end, which HYSPLIT interpolates, cropped when the met
        is. Its output is ``hysplit.log`` in *workdir*, returned as the run's
        log. Without *workdir* it runs in a temporary directory. The release
        heights are added by the caller, as for any model
        (:func:`runtraj.models.run_model`).

        Raises
        ------
        SimulationError
            With HYSPLIT's log in ``log``, and ``reason`` ``TIMEOUT`` when
            the run exceeded *timeout* seconds; the :class:`FailureReason`
            of a known failure message in the log or ``WARNING`` file, such
            as the met running out (``MET_COVERAGE``); ``EXIT_STATUS`` when
            ``hycs_std`` was killed by a signal or exited with a non-zero
            status, whatever it had written; ``PARTICLE_LIMIT``
            when ``maxpar`` stopped a release from several locations before
            the last ones got their particles; ``NO_PARTICLE_DATA`` when
            HYSPLIT wrote no particle file; or ``NO_CONCENTRATION_DATA``
            when a concentration run wrote no file for a grid, or one with
            no sample, as a run shorter than its sampling interval, or fewer
            samples than the run holds, as a run that stopped early. A run whose particles stopped
            before ``n_hours`` well inside the met's domain fails as
            ``MET_COVERAGE`` too, when ``outdt`` is 0: more than 5 grid
            cells from every side of the met grid.
            Particles that all left the met's domain before the end of the
            run are kept.
        MeteorologyError
            Before HYSPLIT runs, when a met file the run needs is missing or
            damaged, or the files do not hold every time the run covers
            (:meth:`Met.check`).
        """
        if workdir is None:
            with tempfile.TemporaryDirectory(prefix="runtraj-") as tmp:
                return self.run(release, config, met, window, Path(tmp), timeout)
        if config.mode == "concentration":
            require_netcdf4()  # before the run, which could not be stored
        files = Met("met", met)
        source = files.files_for(window)
        readable = files.readable(source)
        files.check(readable, window)
        _write_inputs(workdir, release, config, readable)
        try:
            run_hycs_std(workdir, timeout)
            check_release(workdir, release.coords(), config)
            if config.mode == "concentration":
                result: pd.DataFrame | xr.DataTree = _read_grids(workdir, config)
            else:
                result = _read_particles(workdir, config, readable[0])
        except SimulationError as error:
            error.log = _read_log(workdir)
            raise
        # The record names the files found for the run: the archive's, or a
        # crop that served it; the crop settings are in the run's settings.
        return ModelRun(result=result, log=_read_log(workdir), met_files=source)


def _read_particles(
    workdir: Path, config: HysplitConfig, met_file: Path
) -> pd.DataFrame:
    """Return the particles a run in *workdir* wrote, checked for a met that ran out."""
    path = workdir / PARTICLE_FILE
    if not path.exists():
        raise SimulationError(
            f"HYSPLIT wrote no {PARTICLE_FILE}.",
            reason=FailureReason.NO_PARTICLE_DATA,
        )
    particles = read_particle_dat(path, config.varsiwant)
    if config.outdt == 0:  # the check needs a row every time step
        check_met_stop(particles, config.n_hours, _met_grid(met_file))
    return particles


def _read_grids(workdir: Path, config: HysplitConfig) -> xr.DataTree:
    """
    Return the grids a concentration run in *workdir* wrote, a child each, named as in *config*.

    Raises
    ------
    SimulationError
        With ``reason`` ``NO_CONCENTRATION_DATA`` when a grid has no file,
        or its file holds no sample, or fewer samples than a whole run
        writes (:func:`_samples_expected`).
    """
    grids = {}
    for grid in config.grids:
        path = workdir / cdump_name(grid.name)
        if not path.exists():
            raise SimulationError(
                f"HYSPLIT wrote no {path.name} for grid {grid.name!r}.",
                reason=FailureReason.NO_CONCENTRATION_DATA,
            )
        try:
            ds = read_cdump(path, config.cmass)
        except ValueError as error:
            raise SimulationError(
                str(error), reason=FailureReason.NO_CONCENTRATION_DATA
            ) from error
        if ds.sizes["time"] == 0:
            raise SimulationError(
                f"HYSPLIT wrote no sample to grid {grid.name!r}: its "
                f"sampling_interval ({grid.sampling_interval} min) may be "
                f"longer than the run ({abs(config.n_hours)} h).",
                reason=FailureReason.NO_CONCENTRATION_DATA,
            )
        expected = _samples_expected(grid, config.n_hours)
        if ds.sizes["time"] < expected:
            raise SimulationError(
                f"HYSPLIT wrote {ds.sizes['time']} of the {expected} samples "
                f"grid {grid.name!r} holds for a {abs(config.n_hours)} h run: "
                "it stopped before the end of the run.",
                reason=FailureReason.NO_CONCENTRATION_DATA,
            )
        grids[grid.name] = ds
    return xr.DataTree.from_dict(grids)


def _samples_expected(grid: ConcentrationGrid, n_hours: int) -> int:
    """
    Return how many samples HYSPLIT writes to *grid* in a run that reaches its end.

    ``CONDSK`` writes a sample each time the run passes a multiple of the
    sampling interval, counted from the start of the run (the grid's
    sampling start is the run's start, and its stop the run's end), in
    either direction. The part of the run after the last whole interval
    has none. The grid's interval is the one HYSPLIT uses: the config
    refuses one that HYSPLIT would change (an hour or more that is not a
    whole number of hours).
    """
    return abs(n_hours) * 60 // grid.sampling_interval


def _write_inputs(
    workdir: Path, release: Release, config: HysplitConfig, met_files: list[Path]
) -> None:
    """
    Write HYSPLIT's input files for *release* into *workdir*.

    The one place a release becomes what HYSPLIT's ``CONTROL`` holds: its
    time is the release time, its release points are the release
    locations, and its ``altitude_ref`` is ``KMSL`` (0 above ground, 1
    above sea level).
    """
    write_inputs(
        workdir,
        release_time=release.time,
        locations=release.coords(),
        config=config,
        met_files=met_files,
        kmsl=1 if release.altitude_ref == "msl" else 0,
    )


def _met_grid(path: Path) -> Grid:
    """Return the horizontal grid of the ARL file at *path*."""
    from arlmet import File

    with File(path) as met:
        return met.grid


def _read_log(workdir: Path) -> str:
    """Return HYSPLIT's log in *workdir*, or an empty string when it wrote none."""
    path = workdir / LOG_FILE
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


__all__ = ["HysplitModel", "hysplit_version"]

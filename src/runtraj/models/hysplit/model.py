"""HYSPLIT as a transport model."""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import xarray as xr

from runtraj.concentration import require_netcdf4
from runtraj.exceptions import SimulationError
from runtraj.models import ModelRun, TransportModel

from .cdump import read_cdump
from .config import ConcentrationGrid, HysplitConfig
from .control import TDUMP_FILE, cdump_name
from .driver import (
    LOG_FILE,
    PARTICLE_FILE,
    _stamp,
    check_met_stop,
    check_release,
    data_tables,
    find_program,
    hysplit_version,
    program_name,
    read_particle_dat,
    run_hycs_std,
    write_inputs,
)
from .failures import FailureReason
from .met import HysplitMetConfig, Met
from .releases import check_releases, release_rows
from .tdump import read_tdump

if TYPE_CHECKING:
    import pandas as pd
    from arlmet import Grid

    from runtraj.releases import Release


@functools.lru_cache(maxsize=16)
def _file_sha256(path: str, mtime_ns: int, size: int) -> str:
    """Return the SHA-256 of the file at *path*, read once per file."""
    return _sha256(Path(path))


def _sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class HysplitModel(TransportModel[HysplitConfig]):
    """
    HYSPLIT as a transport model, the object ``get_model("hysplit")`` returns.

    :meth:`run` picks the met files a release needs, writes HYSPLIT's input
    files (:func:`~runtraj.models.hysplit.write_inputs`), runs ``hycs_std``,
    and reads the particles it writes
    (:func:`~runtraj.models.hysplit.read_particle_dat`), or, in the
    concentration mode, the file of each grid
    (:func:`~runtraj.models.hysplit.read_cdump`). In the trajectory mode
    it runs ``hyts_std`` and reads its trajectory file
    (:func:`~runtraj.models.hysplit.read_tdump`). HYSPLIT reads the
    met where it is (the cropped copies when the met is cropped), so nothing
    is staged per run.
    """

    name = "hysplit"
    config_class = HysplitConfig
    #: HYSPLIT reads ARL files, found, downloaded, or cropped as a met says.
    met_config_class = HysplitMetConfig

    def result(self, config: HysplitConfig) -> str:
        """
        Return the type of result a run with *config* makes, which its ``mode`` says.

        ``particles`` in ``mode: particle``, ``concentration``, or
        ``trajectories`` in ``mode: trajectory``.

        Examples
        --------
        >>> HysplitModel().result(HysplitConfig(n_hours=-24))
        'particles'
        >>> HysplitModel().result(HysplitConfig(n_hours=-24, mode="trajectory"))
        'trajectories'
        """
        types = {"particle": "particles", "trajectory": "trajectories"}
        return types.get(config.mode, config.mode)

    def result_files(self, config: HysplitConfig) -> tuple[str, ...]:
        """
        Return the files of a run with *config* that hold its result, left out of a failed run's kept workdir.

        The particle file, in concentration mode each grid's
        ``cdump_<name>``, or in trajectory mode ``tdump``.

        Examples
        --------
        >>> HysplitModel().result_files(HysplitConfig(n_hours=-24))
        ('PARTICLE_STILT.DAT',)
        """
        if config.mode == "concentration":
            return tuple(cdump_name(grid.name) for grid in config.grids)
        if config.mode == "trajectory":
            return (TDUMP_FILE,)
        return (PARTICLE_FILE,)

    def sha256(self, config: HysplitConfig) -> str:
        """
        Return the SHA-256 of the program *config* runs, which tells its build from any other.

        The ``hycs_std`` of ``config.directory``, or else on ``PATH``
        (:func:`find_program`); ``hyts_std`` in the trajectory mode. Read
        once per file.

        Raises
        ------
        HYSPLITNotFoundError
            If there is no such program.
        """
        return _file_sha256(
            *_stamp(find_program(program_name(config), config.directory))
        )

    def version(self, config: HysplitConfig) -> str | None:
        """Return the version the program *config* runs prints when it starts (:func:`hysplit_version`)."""
        return hysplit_version(config.directory, program_name(config))

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
            for name, table in sorted(
                data_tables(config.directory, program_name(config)).items()
            )
        }

    def check_releases(
        self, releases: pd.DataFrame, config: HysplitConfig
    ) -> pd.DataFrame:
        """
        Return the releases HYSPLIT cannot run with *config*.

        Multipoint releases whose points share a location, which
        ``hycs_std`` joins into a line source; in the trajectory mode,
        column releases, since ``hyts_std`` has no line source; and in a
        concentration run with ``ichem: 1``, column releases too, which
        are one source at two locations. See
        :func:`runtraj.models.hysplit.releases.check_releases`.
        """
        return check_releases(
            releases,
            trajectory=config.mode == "trajectory",
            sources=config.mode == "concentration" and config.ichem == 1,
        )

    def release_rows(
        self, particles: pd.DataFrame, release: Release, config: HysplitConfig
    ) -> pd.DataFrame:
        """
        Return where each particle started, for a HYSPLIT build that writes no row at ``age = 0``.

        See :func:`runtraj.models.hysplit.releases.release_rows`.
        """
        return release_rows(particles, release, config)

    def run(
        self,
        release: Release,
        config: HysplitConfig,
        met: HysplitMetConfig,
        window: tuple[dt.datetime, dt.datetime],
        workdir: Path | None = None,
        timeout: int | None = None,
    ) -> ModelRun:
        """
        Run HYSPLIT for one release in *workdir* and return its particles, its grids in the concentration mode, or its trajectories.

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
            samples than the run holds, as a run that stopped early;
            ``NO_TRAJECTORIES_DATA`` when a trajectory run wrote no
            trajectory file, or one with no row; or ``MET_COVERAGE`` when
            it followed fewer trajectories than the release has locations,
            which is what ``hyts_std`` does with a location outside the
            met's domain. A run whose particles stopped
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
        result: pd.DataFrame | xr.DataTree
        try:
            if config.mode == "trajectory":
                run_hycs_std(workdir, timeout, program=program_name(config))
                result = _read_trajectories(workdir, len(release.coords()))
                return ModelRun(result=result, log=_read_log(workdir), met_files=source)
            run_hycs_std(workdir, timeout)
            check_release(workdir, release.coords(), config)
            if config.mode == "concentration":
                result = _read_grids(workdir, config)
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


def _read_trajectories(workdir: Path, locations: int) -> pd.DataFrame:
    """
    Return the trajectories a run in *workdir* wrote, one from each of the release's *locations*.

    Raises
    ------
    SimulationError
        With ``reason`` ``NO_TRAJECTORIES_DATA`` when there is no
        trajectory file, or it holds no row; ``MET_COVERAGE`` when it holds
        fewer trajectories than *locations*: ``hyts_std`` leaves out a
        location outside the met's domain, goes on, and numbers the rest
        from 1, so they could not be told apart.
    """
    path = workdir / TDUMP_FILE
    if not path.exists():
        raise SimulationError(
            f"HYSPLIT wrote no {TDUMP_FILE}.",
            reason=FailureReason.NO_TRAJECTORIES_DATA,
        )
    try:
        table = read_tdump(path)
    except ValueError as error:
        raise SimulationError(
            str(error), reason=FailureReason.NO_TRAJECTORIES_DATA
        ) from error
    if table.empty:
        raise SimulationError(
            f"HYSPLIT wrote no trajectory to {TDUMP_FILE}.",
            reason=FailureReason.NO_TRAJECTORIES_DATA,
        )
    followed = table.attrs["trajectories"]
    if followed != locations:
        raise SimulationError(
            f"HYSPLIT followed {followed} of the release's {locations} "
            "trajectories: it leaves out a location outside the met's domain.",
            reason=FailureReason.MET_COVERAGE,
        )
    table.attrs = {}  # the header; a table with attrs does not concatenate quietly
    return table


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
            ds = read_cdump(
                path,
                config.cmass,
                config.ichem or 0,
                pollutant=config.pollutants[0].name,
            )
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

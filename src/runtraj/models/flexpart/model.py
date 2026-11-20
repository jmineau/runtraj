"""FLEXPART 11 as a transport model."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import os
import resource
import signal
import subprocess
import tempfile
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

from runtraj.exceptions import SimulationError
from runtraj.models import ModelRun

from .config import FlexpartConfig, FlexpartMetConfig, flexpart_version
from .inputs import write_inputs
from .met import catalog, files_for, open_era5, open_gfs
from .output import read_partoutput

if TYPE_CHECKING:
    import xarray as xr

    from runtraj.releases import Release
    from runtraj.spatial import Bounds

#: FLEXPART's output, kept in the workdir as the run's log.
LOG_FILE = "flexpart.log"

#: The line FLEXPART ends a run that worked with.
SUCCESS = "CONGRATULATIONS"

#: The lines of FLEXPART's output kept in a failure's message.
TAIL_LINES = 20


#: The warning a FLEXPART run on GFS gives. FLEXPART 11 works out GFS's
#: surface heat flux and stress from the 2 m and 10 m values against its
#: lowest level, which GFS diagnoses from that same layer, so they come out
#: near zero: at a mountain site at midday, 0 W m-2 and 0.0001 N m-2
#: against ERA5's -228 W m-2 and 0.40 N m-2. With no turbulence the
#: particles stayed at a median 8 m in a 1000 m mixed layer.
GFS_WARNING = (
    "FLEXPART 11 does not mix its particles on GFS: it derives GFS's surface "
    "heat flux and stress from the 2 m and 10 m values, which come out near "
    "zero, so the particles stay near the ground. Run on ERA5 for results "
    "that depend on mixing."
)


class FlexpartModel:
    """
    FLEXPART 11 as a transport model, the object ``get_model("flexpart")`` returns.

    :meth:`run` picks the GRIB files a release needs, writes FLEXPART's
    input files into the workdir (:func:`~runtraj.models.flexpart.inputs.write_inputs`),
    runs the checkout's executable there, and reads its particle output
    (:func:`~runtraj.models.flexpart.read_partoutput`). The particles
    carry FLEXPART's ``mlht`` and ``dens`` at each particle, for a stage to
    read. :meth:`open_met` opens a run's GFS or ERA5 files for
    :mod:`runtraj.sampling`, to read other met at the particles.
    """

    name = "flexpart"
    config_class = FlexpartConfig
    #: FLEXPART reads GRIB files, by valid time.
    met_config_class = FlexpartMetConfig
    #: FLEXPART writes its inputs and outputs as files in a directory.
    needs_workdir = True
    #: One FLEXPART process per release.
    batched = False

    def sha256(self, config: FlexpartConfig) -> str:
        """
        Return the SHA-256 of the FLEXPART executable *config* runs, which tells its build from any other.

        Raises
        ------
        FileNotFoundError
            If the checkout has no such executable.
        ValueError
            If ``config.directory`` is not set.
        """
        path = config.executable
        if not path.is_file():
            raise FileNotFoundError(
                f"No FLEXPART executable at {path}: build it in the checkout's "
                f"src/ with make eta={'yes' if config.eta else 'no'}."
            )
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def version(self, config: FlexpartConfig) -> str | None:
        """Return the version of the checkout in ``config.directory``, recorded but not hashed; ``None`` when it has none."""
        try:
            return flexpart_version(config.checkout)
        except FileNotFoundError:
            return None

    def data_files(self, config: FlexpartConfig) -> dict[str, str] | None:
        """Return ``None``: the land-use and surface tables are the checkout's, part of its version."""
        return None

    def open_met(
        self,
        met: FlexpartMetConfig,
        files: list[Path],
        bounds: Bounds | None = None,
    ) -> xr.Dataset:
        """
        Return the GRIB *files* a run read as one Dataset in :func:`arlmet.open_dataset`'s layout, cropped to *bounds*.

        The met config says what the files are, so that the met can be read
        at the particles (:mod:`runtraj.sampling`). GFS files are converted
        with :func:`arlmet.converters.from_gfs`
        (:func:`~runtraj.models.flexpart.met.open_gfs`). ERA5 files, as
        flex_extract writes them, are converted with
        :func:`arlmet.converters.from_flex_extract`
        (:func:`~runtraj.models.flexpart.met.open_era5`); they are already
        cropped to their domain, so *bounds* is not used, and they hold no
        boundary layer height.
        """
        if met.source == "era5":
            return open_era5(files)
        return open_gfs(files, bounds)

    def run(
        self,
        release: Release,
        config: FlexpartConfig,
        met: FlexpartMetConfig,
        window: tuple[dt.datetime, dt.datetime],
        workdir: Path | None = None,
        timeout: int | None = None,
    ) -> ModelRun:
        """
        Run FLEXPART for one release in *workdir* and return its particles.

        Raises
        ------
        MeteorologyError
            Before FLEXPART runs, when the met files do not cover *window*
            or one between is missing.
        SimulationError
            With FLEXPART's output in ``log``: ``reason`` ``TIMEOUT`` when
            the run exceeded *timeout* seconds, ``EXIT_STATUS`` when
            FLEXPART stopped without finishing, and ``NO_PARTICLE_DATA``
            when it finished but wrote no particle file.
        ValueError
            If the met is GFS and the config runs the eta build: FLEXPART's
            documentation says the eta build is intended for ECMWF data.
            ERA5 runs on either build.

        Warns
        -----
        UserWarning
            If the met is GFS, once in each process: FLEXPART 11 does not
            mix its particles on GFS.
        """
        if met.source == "gfs" and config.eta:
            raise ValueError(
                "FLEXPART's eta build is intended for ECMWF data; GFS runs on "
                "src/FLEXPART: set eta: false in the flexpart block."
            )
        if met.source == "gfs":
            warnings.warn(GFS_WARNING, UserWarning, stacklevel=2)
        if workdir is None:
            with tempfile.TemporaryDirectory(prefix="runtraj-") as tmp:
                return self.run(release, config, met, window, Path(tmp), timeout)
        met_files = files_for(catalog(met.directory, met.pattern), window)
        write_inputs(workdir, release, config, met, window, met_files)
        try:
            run_flexpart(workdir, config, timeout)
            outputs = sorted((workdir / "output").glob("partoutput_*.nc"))
            if not outputs:
                raise SimulationError(
                    "FLEXPART wrote no partoutput file.", reason="NO_PARTICLE_DATA"
                )
            particles = read_partoutput(outputs, release.time)
        except SimulationError as error:
            error.log = _read_log(workdir)
            raise
        return ModelRun(
            result=particles,
            log=_read_log(workdir),
            met_files=list(met_files.values()),
        )


def run_flexpart(
    workdir: Path, config: FlexpartConfig, timeout: int | None = None
) -> None:
    """
    Run the checkout's FLEXPART build (``config.executable``) in *workdir*, writing its output to ``flexpart.log``.

    It runs with ``config.threads`` OpenMP threads and its stack limit
    raised as far as the system allows (FLEXPART's documentation asks for
    ``ulimit -s unlimited``: with OpenMP, a small stack ends the run in a
    segmentation fault). ``OMP_PLACES`` and ``OMP_PROC_BIND``, which the
    documentation also suggests, are left as they are: several FLEXPART
    processes on one node would each bind to the same cores. If this process is interrupted while it waits,
    FLEXPART is stopped too.

    Raises
    ------
    SimulationError
        ``TIMEOUT`` when it ran longer than *timeout* seconds, or
        ``EXIT_STATUS`` when it ended without its closing line, with the
        last lines of its output.
    """
    log_path = workdir / LOG_FILE
    env = {**os.environ, "OMP_NUM_THREADS": str(config.threads)}
    with (
        log_path.open("w", encoding="utf-8") as handle,
        subprocess.Popen(
            [str(config.executable), "pathnames"],
            cwd=workdir,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            preexec_fn=_raise_stack_limit,
        ) as proc,
    ):
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            _terminate(proc)
            raise SimulationError(
                f"FLEXPART ran longer than the {timeout} s timeout.", reason="TIMEOUT"
            ) from e
        except BaseException:
            # FLEXPART runs in a session of its own, so no signal reached it.
            _terminate(proc)
            raise
    output = log_path.read_text(encoding="utf-8", errors="replace")
    if proc.returncode != 0 or SUCCESS not in output:
        tail = "\n".join(output.strip().splitlines()[-TAIL_LINES:])
        raise SimulationError(
            f"FLEXPART stopped with exit status {proc.returncode}:\n{tail}",
            reason="EXIT_STATUS",
        )


def _raise_stack_limit() -> None:
    """
    Raise this process's stack limit to its hard limit, as ``ulimit -s unlimited`` does where that is allowed.

    Where the system refuses, FLEXPART runs with the limit it has: macOS
    reports no hard limit and still refuses a stack above 64 MB.
    """
    _, hard = resource.getrlimit(resource.RLIMIT_STACK)
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_STACK, (hard, hard))


def _terminate(proc: subprocess.Popen) -> None:
    """Stop FLEXPART's process group, and wait for it."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def _read_log(workdir: Path) -> str:
    """Return FLEXPART's output in *workdir*, or an empty string when there is none."""
    path = workdir / LOG_FILE
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


__all__ = ["FlexpartModel", "run_flexpart"]

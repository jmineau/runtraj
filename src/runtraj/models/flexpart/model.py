"""FLEXPART 11 as a transport model."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import os
import signal
import subprocess
import tempfile
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
            If the met is ERA5 and the config does not run the eta build.
        """
        if met.source == "era5" and not config.eta:
            raise ValueError(
                "FLEXPART reads ERA5 only with its eta build: set eta: true in "
                "the flexpart block and build src/FLEXPART_ETA."
            )
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

    It runs with ``config.threads`` OpenMP threads. If this process is
    interrupted while it waits, FLEXPART is stopped too.

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

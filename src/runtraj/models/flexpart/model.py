"""FLEXPART 11 as a transport model."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from runtraj._process import read_log, run_program
from runtraj.exceptions import SimulationError
from runtraj.models import ModelRun, TransportModel
from runtraj.particles.table import column_release_rows
from runtraj.releases import ColumnRelease, MultiPointRelease

from .config import FlexpartConfig, FlexpartMetConfig, flexpart_version
from .inputs import split, write_inputs
from .met import catalog, files_for
from .output import read_partoutput

if TYPE_CHECKING:
    from runtraj.releases import Release

#: FLEXPART's output, kept in the workdir as the run's log.
LOG_FILE = "flexpart.log"

#: The line FLEXPART ends a run that worked with.
SUCCESS = "CONGRATULATIONS"

#: The lines of FLEXPART's output kept in a failure's message.
TAIL_LINES = 20


class FlexpartModel(TransportModel[FlexpartConfig]):
    """
    FLEXPART 11 as a transport model, the object ``get_model("flexpart")`` returns.

    :meth:`run` picks the GRIB files a release needs, writes FLEXPART's
    input files into the workdir (:func:`~runtraj.models.flexpart.inputs.write_inputs`),
    runs the checkout's executable there, and reads its particle output
    (:func:`~runtraj.models.flexpart.read_partoutput`). The particles
    carry FLEXPART's ``mlht`` and ``dens`` at each particle, for a stage to
    read. Its met config's :meth:`~runtraj.models.flexpart.FlexpartMetConfig.open`
    opens a run's ERA5 files for :mod:`runtraj.sampling`, to read other met
    at the particles.
    """

    name = "flexpart"
    config_class = FlexpartConfig
    #: FLEXPART reads GRIB files, by valid time.
    met_config_class = FlexpartMetConfig

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

    def release_rows(
        self, particles: pd.DataFrame, release: Release, config: FlexpartConfig
    ) -> pd.DataFrame:
        """
        Return where each particle of a column or multipoint release started, which FLEXPART writes no row for.

        FLEXPART numbers particles from 1 in the order they are released,
        and ``RELEASES`` (:func:`~runtraj.models.flexpart.inputs.releases`)
        releases a column's particle *k* in slab *k* of ``numpar``, and a
        multipoint release's particles point by point, so the number says
        where each started. ``numpar`` is taken to be the highest
        ``particle`` in *particles*: a run whose last-released particles
        left the met's domain before the first output step has them missing,
        and the slabs or points of the others shift.

        Raises
        ------
        ValueError
            If *release* is not a column or multipoint release.
        """
        numpar = int(particles["particle"].max())
        if isinstance(release, ColumnRelease):
            return column_release_rows(release, numpar, numpar)
        if isinstance(release, MultiPointRelease):
            point = np.repeat(
                np.arange(len(release.longitudes)),
                split(numpar, len(release.longitudes)),
            )
            return pd.DataFrame(
                {
                    "particle": np.arange(1, numpar + 1),
                    "lon": np.asarray(release.longitudes, dtype=float)[point],
                    "lat": np.asarray(release.latitudes, dtype=float)[point],
                    "height": np.asarray(release.altitudes, dtype=float)[point],
                }
            )
        raise ValueError(f"A {release.kind} release needs no release rows.")

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
        """
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
            error.log = read_log(workdir / LOG_FILE)
            raise
        return ModelRun(
            result=particles,
            log=read_log(workdir / LOG_FILE),
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
    FLEXPART is stopped too, and if it is killed outright, as by the
    out-of-memory killer, a watchdog ends FLEXPART
    (:func:`runtraj._process.ended_if_this_process_dies`).

    Raises
    ------
    SimulationError
        ``TIMEOUT`` when it ran longer than *timeout* seconds, or
        ``EXIT_STATUS`` when it ended without its closing line, with the
        last lines of its output.
    """
    if sys.platform == "win32":
        raise SimulationError(
            "FLEXPART runs on Linux and macOS: runtraj does not run it on Windows."
        )
    log_path = workdir / LOG_FILE
    try:
        status = run_program(
            [str(config.executable), "pathnames"],
            cwd=workdir,
            log_path=log_path,
            timeout=timeout,
            env={**os.environ, "OMP_NUM_THREADS": str(config.threads)},
            preexec_fn=_raise_stack_limit,
        )
    except subprocess.TimeoutExpired as e:
        raise SimulationError(
            f"FLEXPART ran longer than the {timeout} s timeout.", reason="TIMEOUT"
        ) from e
    output = read_log(log_path)
    if status != 0 or SUCCESS not in output:
        tail = "\n".join(output.strip().splitlines()[-TAIL_LINES:])
        raise SimulationError(
            f"FLEXPART stopped with exit status {status}:\n{tail}",
            reason="EXIT_STATUS",
        )


def _raise_stack_limit() -> None:
    """
    Raise this process's stack limit to its hard limit, as ``ulimit -s unlimited`` does where that is allowed.

    Where the system refuses, FLEXPART runs with the limit it has: macOS
    reports no hard limit and still refuses a stack above 64 MB.
    """
    import resource  # POSIX only; FLEXPART does not run on Windows

    _, hard = resource.getrlimit(resource.RLIMIT_STACK)
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_STACK, (hard, hard))


__all__ = ["FlexpartModel", "run_flexpart"]

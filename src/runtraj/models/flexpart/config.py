"""
FLEXPART's settings and its met.

Settings keep FLEXPART's own names, lowercase (``loutstep``, ``ctl``,
``lconvection``), as they appear in its ``COMMAND`` file, so a FLEXPART
user finds them where they expect.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from runtraj.models import TransportConfig


class FlexpartConfig(TransportConfig):
    """
    FLEXPART 11's settings for a runtraj run.

    runtraj runs the executable of a FLEXPART checkout you built
    (``directory``) and never ships FLEXPART, which is GPL-3. Each run
    writes FLEXPART's input files into its workdir, with particle output
    every ``loutstep`` seconds and no gridded output. The SHA-256 of the
    executable is part of the run's identity, as ``hycs_std``'s is for
    HYSPLIT; the checkout's version is recorded beside it.
    """

    model_config = ConfigDict(extra="forbid")

    #: Where the build is changes no particle; its SHA-256 is hashed instead.
    UNRECORDED: ClassVar[frozenset[str]] = frozenset({"directory"})

    directory: Path | None = Field(
        None,
        description=(
            "Your FLEXPART 11 checkout: runtraj runs ``src/FLEXPART`` (built "
            "with ``make eta=no``) or ``src/FLEXPART_ETA`` (``make eta=yes``), "
            "and links the land-use and species files FLEXPART reads from "
            "``options/``. Needed to run; a stored run's settings leave it out."
        ),
    )
    eta: bool = Field(
        False,
        description=(
            "Run the eta build, ``src/FLEXPART_ETA``, which keeps particles on "
            "ECMWF's model levels and is the build FLEXPART's documentation "
            "intends for ECMWF input (ERA5 from flex_extract). Off runs "
            "``src/FLEXPART``, the meter build, which GFS needs and which "
            "reads ERA5 too. Either build needs ``numpf = 3`` in "
            "``par_mod.f90`` to read flex_extract's three precipitation fields."
        ),
    )
    numpar: int = Field(
        1000,
        description=(
            "Number of particles released. A multipoint release splits them "
            "between its points."
        ),
    )
    loutstep: int = Field(
        3600,
        description="Seconds between particle outputs (``LOUTSTEP``).",
    )
    lsynctime: int = Field(
        900,
        description=(
            "Seconds between the times all particles are synchronized "
            "(``LSYNCTIME``). It must divide ``loutstep``."
        ),
    )
    ctl: float = Field(
        -5.0,
        description=(
            "Time step control (``CTL``): above 1, the boundary-layer time "
            "step is the Lagrangian time scale over ``ctl``; negative uses "
            "``lsynctime``."
        ),
    )
    ifine: int = Field(
        4,
        description=(
            "Reduction of the time step for vertical motion (``IFINE``), used "
            "only when ``ctl`` is above 1."
        ),
    )
    lconvection: Literal[0, 1] = Field(
        1, description="Convection scheme (``LCONVECTION``): 1 on, 0 off."
    )
    lturbulence: Literal[0, 1] = Field(
        1, description="Turbulence scheme (``LTURBULENCE``): 1 on, 0 off."
    )
    lsubgrid: Literal[0, 1] = Field(
        0,
        description=(
            "Raise the mixed layer for subgrid terrain (``LSUBGRID``): 1 on, 0 off."
        ),
    )
    cblflag: Literal[0, 1] = Field(
        0,
        description=(
            "Skewed rather than Gaussian turbulence in the convective boundary "
            "layer (``CBLFLAG``): 1 on, 0 off. It needs short time steps: "
            "``ctl`` of 5 or more, ``ctl`` times ``ifine`` of 50 or more, and "
            "``lsynctime`` of 1200 s or less. FLEXPART changes the three "
            "itself otherwise, so runtraj refuses them."
        ),
    )
    threads: int = Field(
        1,
        description=(
            "OpenMP threads. FLEXPART seeds each thread's random numbers with "
            "a fixed number, so the thread count changes the particles and is "
            "recorded with the run. Each release runs FLEXPART with this many "
            "threads, whatever ``OMP_NUM_THREADS`` is, so ask for ``threads`` "
            "cores for every release that runs at once (``cpus``)."
        ),
    )

    @field_validator("seed")
    @classmethod
    def _no_seed(cls, seed: int | None) -> int | None:
        """Refuse a seed, which FLEXPART has no setting for."""
        if seed is not None:
            raise ValueError(
                "FLEXPART has no seed: it seeds each OpenMP thread with a fixed "
                "number, so a run is repeated by its thread count (threads)."
            )
        return seed

    @field_validator("numpar", "loutstep", "lsynctime", "threads")
    @classmethod
    def _positive(cls, value: int) -> int:
        """Refuse a count or a step that is not positive."""
        if value < 1:
            raise ValueError("must be 1 or more.")
        return value

    @model_validator(mode="after")
    def _steps(self) -> FlexpartConfig:
        """Check that ``lsynctime`` divides ``loutstep``, and that ``cblflag`` has the time steps FLEXPART would force."""
        if self.loutstep % self.lsynctime:
            raise ValueError(
                f"lsynctime ({self.lsynctime} s) must divide loutstep "
                f"({self.loutstep} s)."
            )
        if self.cblflag and (
            self.ctl < 5 or self.ctl * self.ifine < 50 or self.lsynctime > 1200
        ):
            raise ValueError(
                "cblflag: 1 needs ctl of 5 or more, ctl * ifine of 50 or more, "
                "and lsynctime of 1200 s or less (such as ctl: 5, ifine: 10). "
                "FLEXPART changes them itself otherwise, and the run's settings "
                "would not be the ones it ran with."
            )
        return self

    @property
    def checkout(self) -> Path:
        """
        The FLEXPART checkout, ``directory``.

        Raises
        ------
        ValueError
            If ``directory`` is not set.
        """
        if self.directory is None:
            raise ValueError(
                "Set directory to your FLEXPART 11 checkout to run FLEXPART."
            )
        return Path(self.directory)

    @property
    def executable(self) -> Path:
        """The FLEXPART build this config runs: ``src/FLEXPART_ETA`` with ``eta``, else ``src/FLEXPART``."""
        return self.checkout / "src" / ("FLEXPART_ETA" if self.eta else "FLEXPART")


def flexpart_version(directory: str | Path) -> str:
    """
    Return the version of the FLEXPART checkout in *directory*.

    A ``version`` file in the checkout wins; otherwise the git commit, with
    ``-dirty`` when the source has uncommitted changes. It is recorded with
    a run but not hashed: the executable's SHA-256 is.

    Raises
    ------
    FileNotFoundError
        If the checkout has neither a ``version`` file nor git history.
    """
    root = Path(directory)
    if (root / "version").is_file():
        return (root / "version").read_text().strip()
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "describe", "--always", "--dirty"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        raise FileNotFoundError(
            f"{root} has no 'version' file and is not a git checkout. Put a "
            "'version' file in it holding the FLEXPART version (such as "
            "11.0), so its runs are told apart from other builds'."
        ) from None
    return f"git {commit}"


class FlexpartMetConfig(BaseModel):
    """
    The meteorology FLEXPART reads: a directory of GRIB files and what they are.

    FLEXPART reads each file as ``AVAILABLE`` lists it, by valid time; runtraj
    reads the valid time from each file's first message, so the names do
    not matter. Two sources: GFS in GRIB2 on a regular latitude-longitude
    grid (``pgrb2``, analyses and short forecasts), and ERA5 as flex_extract
    writes it (``EA*`` files on model levels).

    A string in ``config.yaml`` is the ``directory``: ``gfs: /path/to/gfs``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["gfs", "era5"] = Field(
        "gfs",
        description=(
            "The weather model the files come from: ``gfs``, NCEP's GFS on "
            "pressure levels, or ``era5``, ECMWF's reanalysis on model levels "
            "as flex_extract writes it, which we run with ``eta: true`` in "
            "the model's block. On GFS, FLEXPART 11 derives the surface heat flux "
            "and stress itself, and at a mountain site at midday they came "
            "out near zero, so its particles did not mix: a run on GFS warns "
            "so, and results that depend on mixing need ERA5."
        ),
    )
    directory: Path = Field(
        ...,
        description=(
            "Directory of the GRIB files. A relative path starts from the "
            "project directory."
        ),
    )
    pattern: str = Field(
        "*",
        description=(
            "Glob that picks the GRIB files in ``directory``. A file that "
            "holds no GRIB message, such as an index cfgrib wrote, is left out."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _folder(cls, data: Any) -> Any:
        """Take a bare string or path as the directory."""
        if isinstance(data, str | Path):
            return {"directory": data}
        return data

    def settings(self) -> dict[str, Any]:
        """
        Return what a run records of this met: its source and its grid.

        The grid, such as ``0.5x0.5 deg, 720x361``, is read from the first
        GRIB file, so GFS at 0.5 and at 0.25 degrees run in different settings
        folders. Where the files are kept is not recorded.

        Raises
        ------
        FileNotFoundError
            If ``directory`` holds no GRIB file that matches ``pattern``.
        """
        from runtraj.models.flexpart.met import grib_grid, holds_grib

        files = sorted(
            p for p in Path(self.directory).glob(self.pattern) if p.is_file()
        )
        first = next((p for p in files if holds_grib(p)), None)
        if first is None:
            raise FileNotFoundError(
                f"No GRIB files match {self.pattern!r} in {self.directory}."
            )
        return {"source": self.source, "grid": grib_grid(first)}


__all__ = ["FlexpartConfig", "FlexpartMetConfig", "flexpart_version"]

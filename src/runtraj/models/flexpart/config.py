"""
FLEXPART's settings and its met.

Settings keep FLEXPART's own names, lowercase (``loutstep``, ``ctl``,
``lconvection``), as they appear in its ``COMMAND`` file, so a FLEXPART
user finds them where they expect.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import (
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from runtraj.models import MetConfig, TransportConfig

if TYPE_CHECKING:
    import xarray as xr

    from runtraj.spatial import Bounds


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
            "``src/FLEXPART``, the meter build, which reads ERA5 too. Either "
            "build needs ``numpf = 3`` in "
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
            "recorded with the run. A run with one thread repeats exactly; "
            "a run with several repeats only in distribution. Each release runs FLEXPART with this many "
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
                "number, so a run with threads: 1 repeats exactly."
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


#: Why a ``flexpart`` block refuses GFS. FLEXPART 11 works out GFS's surface
#: heat flux and stress from the 2 m and 10 m values against its lowest
#: level, which GFS diagnoses from that same layer, so they come out near
#: zero: at a mountain site at midday, 0 W m-2 and 0.0001 N m-2 against
#: ERA5's -228 W m-2 and 0.40 N m-2. With no turbulence the particles
#: stayed at a median 8 m in a 1000 m mixed layer.
GFS_REFUSED = (
    "On current GFS pgrb2 files FLEXPART 11 derives the surface heat flux and "
    "stress near zero (the files hold no surface fluxes, so it derives them from "
    "the 2 m and 10 m values), so particles released near the ground stay near "
    "it. Use ERA5, as flex_extract writes it."
)


class FlexpartMetConfig(MetConfig):
    """
    The meteorology FLEXPART reads: a directory of GRIB files.

    FLEXPART reads each file as ``AVAILABLE`` lists it, by valid time; runtraj
    reads the valid time from each file's first message, so the names do
    not matter. Which weather model the files come from is read from the
    first file too (:meth:`source`): ERA5 as flex_extract writes it (``EA*``
    files on model levels) runs, and GFS is refused, because on current GFS
    ``pgrb2`` files FLEXPART 11 derives the surface heat flux and stress near
    zero, so particles released near the ground stay near it
    (:doc:`/reference/flexpart`).

    A string in ``config.yaml`` is the ``directory``: ``era5: /path/to/era5``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

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

    _source: str | None = PrivateAttr(None)

    @model_validator(mode="before")
    @classmethod
    def _folder(cls, data: Any) -> Any:
        """Take a bare string or path as the directory."""
        if isinstance(data, str | Path):
            return {"directory": data}
        return data

    def _first_file(self) -> Path:
        """
        Return the first GRIB file that matches ``pattern`` in ``directory``.

        Raises
        ------
        FileNotFoundError
            If ``directory`` holds no GRIB file that matches ``pattern``.
        """
        from runtraj.models.flexpart.met import holds_grib

        files = sorted(
            p for p in Path(self.directory).glob(self.pattern) if p.is_file()
        )
        first = next((p for p in files if holds_grib(p)), None)
        if first is None:
            raise FileNotFoundError(
                f"No GRIB files match {self.pattern!r} in {self.directory}."
            )
        return first

    def source(self) -> str:
        """
        Return the weather model the files come from, ``era5``, read from the first file once, and kept.

        Raises
        ------
        FileNotFoundError
            If ``directory`` holds no GRIB file that matches ``pattern``.
        ValueError
            If the files are GFS (FLEXPART 11 derives its surface fluxes
            near zero), or are not ERA5.
        """
        if self._source is None:
            from runtraj.models.flexpart.met import grib_source

            source = grib_source(self._first_file())
            if source == "gfs":
                raise ValueError(f"{self.directory} holds GFS. {GFS_REFUSED}")
            self._source = source
        return self._source

    def settings(self) -> dict[str, Any]:
        """
        Return what a run records of this met: its source and its grid.

        Both are read from the first GRIB file: the source by :meth:`source`,
        and the grid, such as ``0.5x0.5 deg, 720x361``, so ERA5 on two grids
        runs in different settings folders. Where the files are kept is not
        recorded.

        Raises
        ------
        FileNotFoundError
            If ``directory`` holds no GRIB file that matches ``pattern``.
        ValueError
            If the files are not ERA5 (:meth:`source`).
        """
        from runtraj.models.flexpart.met import grib_grid

        return {"source": self.source(), "grid": grib_grid(self._first_file())}

    def open(
        self,
        files: Iterable[str | os.PathLike[str]],
        bounds: Bounds | None = None,
    ) -> xr.Dataset:
        """
        Return the GRIB *files* a run read as one Dataset in :func:`arlmet.open_dataset`'s layout.

        The met is read at the particles this way (:mod:`runtraj.sampling`).
        The files are ERA5, since this config refuses any other. ERA5 files,
        as flex_extract writes them, are converted with
        :func:`arlmet.converters.from_flex_extract`
        (:func:`~runtraj.models.flexpart.met.open_era5`); they are already
        cropped to their domain, so *bounds* is not used, and they hold no
        boundary layer height.
        """
        from runtraj.models.flexpart.met import open_era5

        return open_era5(files)


__all__ = ["FlexpartConfig", "FlexpartMetConfig", "flexpart_version"]

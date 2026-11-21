"""
HYSPLIT's config: every setting that shapes what HYSPLIT makes.

:class:`HysplitConfig` holds them. Most are HYSPLIT's own, under HYSPLIT's
names; a few are used by runtraj itself. Its ``mode`` says what a run
makes: particles (HYSPLIT's particle output, ``ICHEM=8``, the default) or concentration
grids, whose pollutants and grids are :class:`Pollutant` and
:class:`ConcentrationGrid` records. Which input file each setting goes to
is the HYSPLIT driver's business (:mod:`runtraj.models.hysplit.driver`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from runtraj.models import TransportConfig

#: Most hourly ZICONTROL factors HYSPLIT can hold (``ZIPRESC(150)`` in
#: hymodelc.F); it reads more without a bounds check.
MAX_ZISCALE_HOURS = 150

#: The wind-error settings, in the order of the lines of HYSPLIT's WINDERR file.
WIND_ERROR_SETTINGS = ("siguverr", "tluverr", "zcoruverr", "horcoruverr")

#: The mixed-layer error settings, in the order of the lines of HYSPLIT's ZIERR file.
ZI_ERROR_SETTINGS = ("sigzierr", "tlzierr", "horcorzierr")

#: The particle variables ``hycs_std`` writes when ``SETUP.CFG`` names none,
#: its own default ``VARSIWANT``, in its order.
HYSPLIT_VARSIWANT = (
    "time",
    "indx",
    "long",
    "lati",
    "zagl",
    "sigw",
    "tlgr",
    "zsfc",
    "temp",
    "samt",
    "foot",
    "shtf",
    "dmas",
    "dens",
    "rhfr",
    "sphu",
    "dswf",
    "wout",
    "mlht",
    "pres",
)

#: ``SETUP.CFG`` entries written with the same value in every run of the
#: particles mode, which no setting changes. ``ICHEM=8`` is HYSPLIT's particle
#: output mode, the only mode in which ``hycs_std`` opens the particle file
#: runtraj reads, and ``IDSP=2`` is the dispersion scheme that writes its rows:
#: with ``IDSP=1`` the file holds only its header (HYSPLIT 5.1.0 and a 2026 build).
FIXED_SETUP: dict[str, Any] = {"ichem": 8, "idsp": 2}

#: ``SETUP.CFG`` entries written in every run of the concentration mode:
#: HYSPLIT's own concentration output (``ICHEM=0``), with its own dispersion
#: scheme (``IDSP`` unset, HYSPLIT's 1).
FIXED_CONCENTRATION_SETUP: dict[str, Any] = {"ichem": 0}


#: Settings only the particles mode reads: the particle file's variables
#: and interval, the height of ``foot`` (``veght``), and the release's length (in the
#: concentration mode each pollutant has its own ``hours``). A concentration
#: run leaves them out: neither written nor recorded, so a ``hysplit`` block
#: that sets them for its particle variants serves a concentration variant too.
PARTICLES_SETTINGS = ("varsiwant", "outdt", "veght", "emisshrs")

#: Settings only the concentration mode reads.
CONCENTRATION_SETTINGS = ("pollutants", "grids")

#: The ``CPACK`` values whose concentration file runtraj reads: 0 every cell,
#: 1 and 3 only the cells that are not zero (written the same way).
CPACK_READ = (0, 1, 3)

#: What one sample of a concentration grid is.
Sampling = Literal["average", "snapshot", "maximum"]

#: HYSPLIT's sampling types, line 25 of each grid in ``CONTROL``.
SAMPLING_TYPES: dict[Sampling, int] = {"average": 0, "snapshot": 1, "maximum": 2}


#: Names a pollutant cannot have: a grid's coordinates, which its variables share a Dataset with.
#: (``time_end`` is a coordinate too, but longer than a pollutant's four characters.)
RESERVED_POLLUTANT_NAMES = frozenset({"lat", "lon", "z", "time"})


class Pollutant(BaseModel):
    """
    One pollutant of a HYSPLIT concentration run: what is released, and how it is removed.

    Every pollutant is released at every release location, and counted on
    every grid. The removal settings are HYSPLIT's deposition lines of
    ``CONTROL``, in HYSPLIT's units; all zero, the default, is a gas that
    is not removed. See the HYSPLIT user guide for each.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(
        ...,
        pattern=r"^[A-Za-z0-9_]{1,4}$",
        description=(
            "Up to four letters or digits: HYSPLIT's identifier, and the name "
            "of the pollutant's variable in each grid. Not ``lat``, ``lon``, "
            "``z`` or ``time``, which name the grid's coordinates."
        ),
    )
    rate: float = Field(
        1.0,
        ge=0,
        description=(
            "Emission rate, per hour, at each release location. Its mass unit "
            "is the concentration's: 1 per hour for an hour gives a mass of 1."
        ),
    )
    hours: float = Field(
        1.0,
        gt=0,
        description="How long the pollutant is released, in hours, from the release time.",
    )
    particle_diameter: float = Field(
        0.0, ge=0, description="Particle diameter, in micrometers. 0 for a gas."
    )
    particle_density: float = Field(
        0.0, ge=0, description="Particle density, in g/cm3. 0 for a gas."
    )
    particle_shape: float = Field(
        0.0, ge=0, description="Particle shape factor. 0 for a gas."
    )
    deposition_velocity: float = Field(
        0.0,
        ge=0,
        description="Dry deposition velocity, in m/s. 0 for none, or to compute it from the settings below.",
    )
    molecular_weight: float = Field(
        0.0, ge=0, description="Molecular weight, in g/mol, for the resistance method."
    )
    surface_reactivity: float = Field(
        0.0, ge=0, description="Surface reactivity ratio, for the resistance method."
    )
    diffusivity_ratio: float = Field(
        0.0, ge=0, description="Diffusivity ratio, for the resistance method."
    )
    henry_effective: float = Field(
        0.0,
        ge=0,
        description="Effective Henry's constant, in M/atm, for the resistance method.",
    )
    henry: float = Field(
        0.0, ge=0, description="Henry's constant for wet removal of a gas, in M/atm."
    )
    in_cloud: float = Field(
        0.0, ge=0, description="In-cloud wet removal, in L/L (particles)."
    )
    below_cloud: float = Field(
        0.0, ge=0, description="Below-cloud wet removal, in 1/s (particles)."
    )
    half_life: float = Field(
        0.0, ge=0, description="Radioactive decay half-life, in days. 0 for none."
    )
    resuspension: float = Field(
        0.0, ge=0, description="Resuspension factor of deposited mass, in 1/m."
    )

    @field_validator("name")
    @classmethod
    def _not_a_coordinate(cls, value: str) -> str:
        """Refuse a name a grid's coordinates use, which its variable cannot have."""
        if value in RESERVED_POLLUTANT_NAMES:
            raise ValueError(
                f"{value!r} is the name of a coordinate of every grid, and a "
                "grid's pollutant is a variable of the same Dataset. Use "
                f"another name, not one of {sorted(RESERVED_POLLUTANT_NAMES)}."
            )
        return value


class ConcentrationGrid(BaseModel):
    """
    One grid of a HYSPLIT concentration run: where and how often the particles are counted.

    A grid is regular in latitude and longitude, with levels of heights
    above ground. HYSPLIT averages, snapshots, or takes the maximum of the
    concentration over each sampling interval, from the start of the run to
    its end.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(
        ...,
        pattern=r"^[A-Za-z0-9_-]{1,32}$",
        description="The grid's name: its group in the result's file, and its child in the tree.",
    )
    center_lon: float | None = Field(
        None,
        ge=-180,
        le=360,
        description=(
            "Longitude of the grid's center, in degrees. Unset with "
            "``center_lat``, the grid is centered on each release."
        ),
    )
    center_lat: float | None = Field(
        None,
        ge=-90,
        le=90,
        description="Latitude of the grid's center, in degrees. Set with ``center_lon``.",
    )
    spacing_lat: float = Field(
        0.05,
        gt=0,
        description="Distance between rows of cells, in degrees of latitude.",
    )
    spacing_lon: float = Field(
        0.05,
        gt=0,
        description="Distance between columns of cells, in degrees of longitude.",
    )
    span_lat: float = Field(
        1.0, gt=0, description="How far the grid reaches in latitude, in degrees."
    )
    span_lon: float = Field(
        1.0, gt=0, description="How far the grid reaches in longitude, in degrees."
    )
    levels: list[int] = Field(
        default_factory=lambda: [100],
        min_length=1,
        description=(
            "Top of each level, in whole meters above ground, from the "
            "bottom. A level of 0 counts deposition on the ground."
        ),
    )
    sampling: Sampling = Field(
        "average",
        description="What each sample is: the average over its interval, the value at its end, or the maximum.",
    )
    sampling_interval: int = Field(
        60,
        gt=0,
        description="Length of each sample, in minutes.",
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Refuse a center with one coordinate, a center HYSPLIT would read as the release, and levels out of order."""
        if (self.center_lon is None) != (self.center_lat is None):
            raise ValueError(
                f"Grid {self.name!r}: set center_lon and center_lat together, "
                "or neither to center the grid on each release."
            )
        if self.center_lon == 0 and self.center_lat == 0:
            raise ValueError(
                f"Grid {self.name!r}: HYSPLIT reads a center of 0, 0 as the "
                "release location. Leave center_lon and center_lat unset for that."
            )
        levels = self.levels
        if any(z < 0 for z in levels) or levels != sorted(set(levels)):
            raise ValueError(
                f"Grid {self.name!r}: levels must be heights of 0 m or more, "
                f"each above the one before, not {levels}."
            )
        return self


class HysplitConfig(TransportConfig):
    """
    HYSPLIT's config: every setting that shapes what it makes.

    In ``config.yaml`` they are the keys of the ``hysplit`` block under
    ``models:``, and a variant may override any of them. Most are HYSPLIT ``SETUP.CFG`` entries with
    HYSPLIT's own names; see the HYSPLIT user guide for the full meaning of
    each. The wind-error group (``siguverr``, ``tluverr``, ``zcoruverr``,
    ``horcoruverr``) perturbs the particles' winds, and the mixed-layer group
    (``sigzierr``, ``tlzierr``, ``horcorzierr``) perturbs each particle's
    ``foot`` by a random mixed-layer height error. Each group is set in full
    or not at all.

    ``ichem`` and ``idsp`` are not settings: runtraj always writes
    ``ICHEM=8`` and ``IDSP=2`` (:data:`FIXED_SETUP`), the only values with
    which HYSPLIT writes particles, and a config that sets one is refused. Some settings act only together with
    another: the puff settings (``conage``, ``kpuff``, ``kspl``, ``krnd``,
    ``splitf`` and the ``fr`` group) when ``initd`` releases puffs, and
    ``khinp`` and ``pinpf`` when ``ninit`` reads particles from a file. A few
    are read only for HYSPLIT's concentration grid or deposition, which
    runtraj does not read, and ``delt`` is overridden in this mode.

    The defaults are HYSPLIT's own for a dispersion run in this mode
    (``ICHEM=8``). HYSPLIT decides ``capemin``, ``cpack``, ``kblt``,
    ``kmixd``, ``krand`` and ``vscales`` only after it has read
    ``SETUP.CFG``, from the mode and ``numpar``, so they are unset by
    default: an unset setting is not written, and HYSPLIT chooses.
    ``n_hours`` has no default.

    ``mode`` says what a run makes. ``particles`` (the default) is the
    particle table, from HYSPLIT's particle output (``ICHEM=8``). ``concentration`` is
    HYSPLIT's own concentration grids (``ICHEM=0``), one grid of each of
    ``grids`` with a variable for each of ``pollutants``. A concentration
    run leaves out the particle file's settings
    (:data:`PARTICLES_SETTINGS`), which a ``hysplit`` block may set for
    its particle variants; ``pollutants`` and ``grids`` without ``mode:
    concentration`` are refused. A particles-mode run
    records no ``mode``, ``pollutants`` or ``grids``, so its settings
    folder is the one it had before the mode existed.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: Fields left out of a run's recorded settings. What ``directory`` holds
    #: is recorded instead: the checksum of ``hycs_std`` and of each data
    #: table.
    UNRECORDED: ClassVar[frozenset[str]] = frozenset({"directory"})

    mode: Literal["particles", "concentration"] = Field(
        "particles",
        description=(
            "What HYSPLIT makes: ``particles``, the particle table, or "
            "``concentration``, grids of each pollutant's concentration "
            "(``grids`` and ``pollutants``)."
        ),
    )
    pollutants: list[Pollutant] = Field(
        default_factory=list,
        description=(
            "The pollutants of a concentration run, each released at every "
            "release location and counted on every grid. At least one in "
            "``mode: concentration``; none otherwise."
        ),
    )
    grids: list[ConcentrationGrid] = Field(
        default_factory=list,
        description=(
            "The grids of a concentration run. At least one in ``mode: "
            "concentration``; none otherwise."
        ),
    )
    numpar: int = Field(
        2500,
        ge=1,
        description=(
            "Number of particles released per simulation. More particles sample "
            "the turbulence better and take longer to run."
        ),
    )
    directory: Path | None = Field(
        None,
        description=(
            "The HYSPLIT folder as unpacked: runtraj runs ``exec/hycs_std`` "
            "under it and reads the data tables (``ASCDATA.CFG``, "
            "``LANDUSE.ASC``, ``ROUGLEN.ASC``, ``TERRAIN.ASC``) from its "
            "``bdyfiles/``. Unset, the first ``hycs_std`` on ``PATH`` runs, with "
            "the tables of a ``bdyfiles/`` folder beside its folder; runtraj "
            "includes neither. The SHA-256 of ``hycs_std`` and of each table is "
            "part of each run's settings, so runs of different builds are kept "
            "apart."
        ),
    )
    varsiwant: list[
        Literal[
            "time",
            "indx",
            "long",
            "lati",
            "zagl",
            "sigw",
            "tlgr",
            "zsfc",
            "icdx",
            "temp",
            "samt",
            "foot",
            "shtf",
            "tcld",
            "dmas",
            "dens",
            "rhfr",
            "sphu",
            "lcld",
            "zloc",
            "dswf",
            "wout",
            "mlht",
            "rain",
            "crai",
            "pres",
            "whtf",
            "temz",
            "zcfx",
        ]
    ] = Field(
        default_factory=lambda: list(HYSPLIT_VARSIWANT),
        description=(
            "Particle variables ``hycs_std`` writes to the trajectory output, "
            "by the lowercase four-letter names HYSPLIT reads. Each is a "
            "column of the particles under the same name, except ``indx``, "
            "``time``, ``long``, and ``lati``. ``zcfx`` is the vertical "
            "displacement by convective flux along the trajectory, in m. "
            "The default is HYSPLIT's own list of 20."
        ),
    )
    capemin: float | None = Field(
        None,
        description=(
            "Convection option. -1 turns convection off, -2 uses the Grell "
            "scheme, and a positive value mixes vertically when CAPE exceeds "
            "it, in J/kg. Unset, HYSPLIT chooses -2 with ``ICHEM=8``."
        ),
    )
    cmass: int = Field(
        0,
        description=(
            "Compute grid concentrations (0) or grid mass (1). "
            "Read only for HYSPLIT's concentration grids (``mode: concentration``), "
            "where 1 makes each cell hold mass, not mass per volume, and "
            "counts no deposition at ``z = 0``."
        ),
    )
    conage: int = Field(
        24,
        description=(
            "Particle age at which particles and puffs convert, in hours, "
            "when ``initd`` is 3 or 4."
        ),
    )
    cpack: int | None = Field(
        None,
        description=(
            "Packing of the binary concentration grid: 0 every point, 1 only "
            "nonzero points. Unset, HYSPLIT chooses 1. Read only for HYSPLIT's "
            "concentration grids (``mode: concentration``), where 2, one "
            "point per grid, is refused: runtraj does not read its file."
        ),
    )
    delt: float = Field(
        0.0,
        description=(
            "Integration time step, in minutes. 0 lets HYSPLIT choose; a "
            "negative value sets the minimum step. With ``ICHEM=8`` HYSPLIT "
            "always steps 1 minute, so this setting has no effect."
        ),
    )
    dxf: float = Field(
        1.0,
        description=(
            "Horizontal x offset factor for HYSPLIT's meteorological ensemble."
        ),
    )
    dyf: float = Field(
        1.0,
        description=(
            "Horizontal y offset factor for HYSPLIT's meteorological ensemble."
        ),
    )
    dzf: float = Field(
        0.01,
        description=(
            "Vertical offset factor for HYSPLIT's meteorological ensemble "
            "(0.01 is about 250 m)."
        ),
    )
    efile: str = Field(
        "",
        description=("Name of a time-varying emissions file. Blank uses none."),
    )
    emisshrs: float = Field(
        0.01,
        ge=0,
        description="Duration of the particle release, in hours.",
    )
    frhmax: float = Field(
        3.0,
        description=(
            "Maximum horizontal puff-rounding parameter. "
            "Used only for puffs, when ``initd`` is not 0."
        ),
    )
    frhs: float = Field(
        1.0,
        description=(
            "Horizontal puff-rounding fraction for merging. "
            "Used only for puffs, when ``initd`` is not 0."
        ),
    )
    frme: float = Field(
        0.1,
        description=(
            "Mass-rounding fraction for enhanced puff merging. "
            "Used only for puffs, when ``initd`` is not 0."
        ),
    )
    frmr: float = Field(
        0.0,
        description=(
            "Mass-removal fraction for enhanced puff merging. "
            "Used only for puffs, when ``initd`` is not 0."
        ),
    )
    frts: float = Field(
        0.1,
        description=(
            "Temporal puff-rounding fraction. "
            "Used only for puffs, when ``initd`` is not 0."
        ),
    )
    frvs: float = Field(
        0.01,
        description=(
            "Vertical puff-rounding fraction. "
            "Used only for puffs, when ``initd`` is not 0."
        ),
    )
    hscale: float = Field(
        10800.0,
        description="Horizontal Lagrangian timescale, in seconds.",
    )
    initd: int = Field(
        0,
        description=(
            "Initial distribution: 0 3D particles, 1 and 2 3D puffs, 3 and "
            "4 puffs horizontally and particles vertically. Other values are "
            "HYSPLIT's conversions and special modes."
        ),
    )
    k10m: int = Field(
        1,
        description=(
            "Use the 10 m winds and 2 m temperature as the lowest meteorology "
            "level (1) or skip them (0)."
        ),
    )
    kagl: int = Field(
        0,
        description=(
            "Trajectory heights above ground (1) or above sea level (0). "
            "Read only by HYSPLIT's trajectory model, not by ``hycs_std``."
        ),
    )
    kbls: int = Field(
        1,
        description=(
            "Derive boundary-layer stability from surface fluxes (1) or from "
            "wind and temperature profiles (2)."
        ),
    )
    kblt: int | None = Field(
        None,
        description=(
            "Boundary-layer turbulence scheme: 1 Beljaars, 2 Kantha-Clayson, "
            "3 TKE, 4 measured variances, 5 Hanna. Unset, HYSPLIT chooses 5 "
            "with ``ICHEM=8``."
        ),
    )
    kdef: int = Field(
        0,
        description="Horizontal turbulence from vertical mixing (0) or wind deformation (1).",
    )
    khinp: int = Field(
        0,
        description=(
            "Age, in hours, given to particles read from ``pinpf``. 0 keeps "
            "their own age."
        ),
    )
    khmax: int = Field(
        9999,
        description=(
            "Maximum particle age, in hours. HYSPLIT drops a particle older than this."
        ),
    )
    kmix0: int = Field(
        150,
        description="Minimum mixed-layer depth, in meters.",
    )
    kmixd: int | None = Field(
        None,
        description=(
            "Mixed-layer depth source: 0 from the meteorology, 1 from the "
            "temperature profile, 2 from the TKE profile, 3 from a modified "
            "Richardson number. Unset, HYSPLIT chooses 3 with ``ICHEM=8``."
        ),
    )
    kpuff: int = Field(
        0,
        description=(
            "Horizontal puff growth: linear (0) or empirical (1). "
            "Used only for puffs, when ``initd`` is not 0."
        ),
    )
    krand: Literal[0, 1, 2, 3, 4, 10, 11, 12, 13] | None = Field(
        None,
        description=(
            "How HYSPLIT draws the random numbers for turbulence. 0, or "
            "unset, picks 2 when ``numpar`` is 5000 or less and 1 otherwise. 1 uses a "
            "precomputed table. 2 draws them during the run and is the only "
            "mode that uses ``seed``. 3 uses no random numbers (a diagnostic "
            "mode). 4 draws them during the run from a clock-based seed, so "
            "every run differs; there are about 5000 possible seeds. 10 to 13 "
            "are modes 0 to 3 with a random initial seed. HYSPLIT does not "
            "check this value and other values silently break the turbulence, "
            "so runtraj rejects them."
        ),
    )
    seed: int | None = Field(
        None,
        description=(
            "Random seed for a reproducible run. Different seeds give "
            "different runs. Requires ``krand: 2``. HYSPLIT 5.1.0 "
            "ignores the seed under ``krand`` 4 and 10 to 13, and under 1 uses "
            "it only for the initial turbulent velocity. Must be 0 or more: "
            "runtraj writes it to ``SETUP.CFG`` as ``-(seed + 1)``, because "
            "HYSPLIT reseeds its generator only from a negative value and "
            "gives every ``SEED`` of 0 or more the same stream, so a seed "
            "and its negative would give the same run. Realization ``k`` of "
            "a variant runs with ``seed + k``, so realization 0 shares the "
            "unperturbed run's seed."
        ),
    )
    krnd: int = Field(
        6,
        description=(
            "Enhanced puff-merging interval, in hours. "
            "Used only for puffs, when ``initd`` is not 0."
        ),
    )
    kspl: int = Field(
        1,
        description=(
            "Standard puff-splitting interval, in hours. "
            "Used only for puffs, when ``initd`` is not 0."
        ),
    )
    kwet: int = Field(
        1,
        description=(
            "Precipitation for wet removal from the meteorology (1) or from "
            "an external file (2). Read only for deposition, which runtraj's "
            "``CONTROL`` turns off."
        ),
    )
    kzmix: int = Field(
        0,
        description=(
            "Vertical mixing adjustment: 0 none, 1 a single PBL-average value, "
            "2 scale by ``tvmix``."
        ),
    )
    maxdim: int = Field(
        1,
        description=("Maximum number of pollutants carried on one particle."),
    )
    maxpar: int | None = Field(
        None,
        description=(
            "Maximum number of particles in a simulation. Unset gives every "
            "release location its full share: ``numpar`` for one location, "
            "``n * (numpar // n + 1)`` for n. A set value that stops a "
            "multipoint release before its last location fails the run."
        ),
    )
    mgmin: int = Field(
        10,
        description="Minimum meteorological subgrid size, in grid points.",
    )
    mhrs: int = Field(
        0,
        description=(
            "Trajectory restart duration limit, in hours. "
            "Read only by HYSPLIT's trajectory model, not by ``hycs_std``."
        ),
    )
    nbptyp: int = Field(
        1,
        description=("Number of particle-size bins per pollutant type."),
    )
    ncycl: int = Field(
        0,
        description=("Cycle time of the particle dump file, in hours."),
    )
    ndump: int = Field(
        0,
        description=(
            "Interval between particle dumps to ``poutf``, in hours. 0 "
            "writes none. runtraj does not keep the dump file."
        ),
    )
    ninit: int = Field(
        1,
        description=(
            "Particle initialization from ``pinpf``: 0 none, 1 once at the "
            "start, 2 add every hour, 3 replace every hour. A file that does "
            "not exist is skipped."
        ),
    )
    nstr: int = Field(
        0,
        description=(
            "Trajectory restart interval, in hours. "
            "Read only by HYSPLIT's trajectory model, not by ``hycs_std``."
        ),
    )
    nturb: int = Field(
        0,
        description="Turbulence on (0) or off (1).",
    )
    nver: int = Field(
        0,
        description=(
            "Trajectory vertical split number. "
            "Read only by HYSPLIT's trajectory model, not by ``hycs_std``."
        ),
    )
    outdt: int = Field(
        0,
        ge=0,
        description=(
            "Interval between particle outputs in the particle file, in "
            "minutes. 0 writes every time step. HYSPLIT writes no particles "
            "for a negative value, so it is refused. "
            "Only at 0 does runtraj check where the particles of a run that "
            "stopped early ended, to tell met that ran out from particles that "
            "left the met's domain."
        ),
    )
    p10f: float = Field(
        1.0,
        description=(
            "Dust threshold velocity sensitivity factor, read only by "
            "HYSPLIT's dust emission module."
        ),
    )
    pinbc: str = Field(
        "PARINBC",
        description=("Particle input file for time-varying boundary conditions."),
    )
    pinpf: str = Field(
        "PARINIT",
        description=("Particle input file read at initialization (``ninit``)."),
    )
    poutf: str = Field(
        "PARDUMP",
        description=("Particle dump output file (``ndump``)."),
    )
    qcycle: float = Field(
        0.0,
        description="Emission cycling period, in hours. 0 turns cycling off.",
    )
    rhb: int = Field(
        80,
        description=(
            "Relative humidity that defines a cloud base, in percent, for wet removal."
        ),
    )
    rht: int = Field(
        60,
        description=(
            "Relative humidity that defines a cloud top, in percent, for wet removal."
        ),
    )
    splitf: float = Field(
        1.0,
        description=(
            "Factor for the automatic horizontal splitting size. A negative "
            "value turns the automatic sizing off. "
            "Used only for puffs, when ``initd`` is not 0."
        ),
    )
    tkerd: float = Field(
        0.18,
        description="Ratio w'²/(u'²+v'²) of TKE components when unstable.",
    )
    tkern: float = Field(
        0.18,
        description="Ratio w'²/(u'²+v'²) of TKE components when stable.",
    )
    tlfrac: float = Field(
        0.1,
        description=(
            "Fraction of the vertical Lagrangian timescale used as the time "
            "step of the dispersion scheme."
        ),
    )
    tout: int = Field(
        0,
        description=(
            "Trajectory output interval, in minutes. "
            "Read only by HYSPLIT's trajectory model, not by ``hycs_std``."
        ),
    )
    tratio: float = Field(
        0.75,
        description=(
            "Advection stability ratio: the fraction of a grid cell a "
            "particle may move in one time step."
        ),
    )
    tvmix: float = Field(
        1.0,
        description="Vertical mixing scale factor, used by the ``kzmix`` scaling modes.",
    )
    veght: float = Field(
        0.5,
        gt=0,
        description=(
            "Height below which HYSPLIT counts a particle's time toward its "
            "``foot``: the depth surface fluxes are mixed through. A value of 1 "
            "or less is a fraction of the mixed-layer height; a larger value is "
            "meters above ground."
        ),
    )
    vscale: float = Field(
        200.0,
        description="Vertical Lagrangian timescale, in seconds.",
    )
    vscaleu: float = Field(
        200.0,
        description="Vertical Lagrangian timescale in an unstable boundary layer, in seconds.",
    )
    vscales: float | None = Field(
        None,
        description=(
            "Vertical Lagrangian timescale in a stable boundary layer, in "
            "seconds. -1 uses the Hanna timescale, which varies with the "
            "turbulence, and then ``vscaleu`` is not used. Unset, HYSPLIT "
            "chooses -1 with ``ICHEM=8``."
        ),
    )
    w_option: int = Field(
        0,
        description=(
            "Vertical motion method: 0 the meteorology's vertical velocity, "
            "1 isobaric, 2 isentropic, 3 constant density, 4 constant sigma."
        ),
    )
    wbbh: float = Field(
        0.0,
        description=(
            "Height at which the fixed vertical velocity switches from rise to "
            "fall, in meters. Used by vertical motion option 9."
        ),
    )
    wbwf: float = Field(
        0.0,
        description="Fixed fall velocity, in m/s. Used by vertical motion options 9 and 10.",
    )
    wbwr: float = Field(
        0.0,
        description="Fixed rise velocity, in m/s. Used by vertical motion option 9.",
    )
    wvert: bool = Field(
        True,
        description="Interpolate WRF fields vertically with the WRF scheme instead of HYSPLIT's.",
    )
    z_top: float = Field(
        25000.0,
        gt=0,
        description="Top of HYSPLIT's computational domain, in meters above ground.",
    )
    ziscale: float | list[float] = Field(
        1.0,
        description=(
            "Factor applied to the mixed-layer height, written to HYSPLIT's "
            "``ZICONTROL`` file. 1.0 leaves it unscaled. A single value applies "
            "to every hour of the run. A list gives one factor per hour from "
            "the release (at most 150), and later hours are unscaled. HYSPLIT "
            "applies ``kmix0`` after the factor, so the mixed layer never drops "
            "below ``kmix0``. A negative value uses the meteorology's own PBL "
            "height where the met files carry one."
        ),
    )
    siguverr: float | None = Field(
        None,
        ge=0,
        description="Standard deviation of the horizontal wind error, in m/s.",
    )
    tluverr: float | None = Field(
        None,
        ge=0,
        description="Correlation timescale of the horizontal wind error, in minutes.",
    )
    zcoruverr: float | None = Field(
        None,
        ge=0,
        description="Vertical correlation length of the horizontal wind error, in meters.",
    )
    horcoruverr: float | None = Field(
        None,
        ge=0,
        description="Horizontal correlation length of the horizontal wind error, in km.",
    )
    sigzierr: float | None = Field(
        None,
        ge=0,
        description="Standard deviation of the mixed-layer height error, in percent.",
    )
    tlzierr: float | None = Field(
        None,
        ge=0,
        description="Correlation timescale of the mixed-layer height error, in minutes.",
    )
    horcorzierr: float | None = Field(
        None,
        ge=0,
        description="Horizontal correlation length of the mixed-layer height error, in km.",
    )

    @field_validator("varsiwant", mode="before")
    @classmethod
    def _refuse_zfx1(cls, value: Any) -> Any:
        """Refuse ``zfx1``, which ``hycs_std`` stops on, and name ``zcfx``, which it reads."""
        if isinstance(value, list) and any(
            isinstance(name, str) and name.lower() == "zfx1" for name in value
        ):
            raise ValueError(
                "'zfx1' is not a varsiwant name: hycs_std stops with 'Weird "
                "variable'. HYSPLIT reads 'zcfx' for the vertical displacement "
                "by convective flux."
            )
        return value

    @field_validator("ziscale", mode="before")
    @classmethod
    def _flatten_ziscale(cls, value: Any) -> Any:
        """
        Read a one-element nested list, ``[[0.8, 0.9]]``, as ``[0.8, 0.9]``.

        Both mean the same factors, so they must hash the same.
        """
        if isinstance(value, list) and value and isinstance(value[0], list):
            if len(value) != 1:
                raise ValueError(
                    "Per-simulation ziscale lists are not supported. Pass one shared "
                    "list of hourly factors for all simulations."
                )
            return value[0]
        return value

    @model_validator(mode="after")
    def _validate_mode(self) -> Self:
        """Refuse pollutants and grids outside the concentration mode, and a concentration run without a pollutant or a grid."""
        if self.mode == "particles":
            used = [n for n in CONCENTRATION_SETTINGS if getattr(self, n)]
            if used:
                raise ValueError(
                    f"{', '.join(used)} are settings of HYSPLIT's mode: "
                    "concentration, and this config's mode is particles. Set "
                    "mode: concentration, or leave them out."
                )
            return self
        for name in CONCENTRATION_SETTINGS:
            records = getattr(self, name)
            if not records:
                raise ValueError(
                    f"HYSPLIT's mode: concentration needs at least one of {name}."
                )
            names = [r.name for r in records]
            twice = sorted({n for n in names if names.count(n) > 1})
            if twice:
                raise ValueError(
                    f"Two of {name} are named {twice}: each needs its own name."
                )
        if self.cpack is not None and self.cpack not in CPACK_READ:
            raise ValueError(
                f"cpack={self.cpack} is not read: runtraj reads HYSPLIT's "
                f"concentration files with cpack {', '.join(map(str, CPACK_READ))}."
            )
        return self

    def settings(self) -> dict[str, Any]:
        """
        Return what a run records of this config: the settings of its mode.

        A particles-mode record has no ``mode``, ``pollutants`` or
        ``grids``, so it is the record (and the settings folder) a run had
        before the mode existed. A concentration-mode record has no
        :data:`PARTICLES_SETTINGS`.
        """
        record = super().settings()
        drop = (
            ("mode", *CONCENTRATION_SETTINGS)
            if self.mode == "particles"
            else PARTICLES_SETTINGS
        )
        for name in drop:
            record.pop(name, None)
        return record

    @model_validator(mode="after")
    def _validate_error_params(self) -> Self:
        """Require each error group to be set in full or not at all."""
        for name, fields in (("XY", WIND_ERROR_SETTINGS), ("ZI", ZI_ERROR_SETTINGS)):
            unset = [getattr(self, f) is None for f in fields]
            if any(unset) and not all(unset):
                raise ValueError(
                    f"Inconsistent {name} error parameters: all must be set or all None"
                )
        return self

    @model_validator(mode="after")
    def _validate_ziscale(self) -> Self:
        """Reject ziscale values that are empty, zero, or longer than 150 hours."""
        hourly = self.hourly_ziscale()
        if not hourly:
            raise ValueError("ziscale cannot be empty; use 1.0 for no scaling.")
        if any(v == 0.0 for v in hourly):
            raise ValueError(
                "ziscale of 0 would collapse the mixed layer to kmix0. "
                "Use 1.0 for no scaling."
            )
        if any(v != 1.0 for v in hourly) and len(hourly) > MAX_ZISCALE_HOURS:
            raise ValueError(
                f"ziscale gives {len(hourly)} hourly factors, but HYSPLIT holds at "
                f"most {MAX_ZISCALE_HOURS}. A scalar ziscale is repeated for "
                "every hour, so it needs abs(n_hours) <= "
                f"{MAX_ZISCALE_HOURS}; for longer runs give a list of up to "
                f"{MAX_ZISCALE_HOURS} factors, after which the mixed layer "
                "is unscaled."
            )
        return self

    @model_validator(mode="after")
    def _validate_seed(self) -> Self:
        """Require a seed of 0 or more, and ``krand=2``, when a seed is set."""
        if self.seed is not None and self.seed < 0:
            raise ValueError(
                f"seed={self.seed} must be 0 or more: runtraj writes the seed "
                "to SETUP.CFG as -(seed + 1) because HYSPLIT reseeds only from "
                "a negative value, so a seed and its negative would give the "
                "same run, and the realizations of a negative seed would "
                "repeat one another. Use a seed of 0 or more."
            )
        if self.seed is not None and self.krand != 2:
            got = "krand unset" if self.krand is None else f"krand={self.krand}"
            raise ValueError(
                f"seed={self.seed} requires krand=2 (got {got}): the "
                "hycs_std of HYSPLIT 5.1.0 discards the namelist seed under krand=4 and "
                "10-13 and uses it only for the initial turbulent velocity under "
                "krand=1. Set krand=2, or drop the seed."
            )
        return self

    def maxpar_for(self, locations: int) -> int:
        """
        Return the ``MAXPAR`` HYSPLIT gets for a release from *locations* horizontal locations.

        HYSPLIT gives each location ``numpar // locations`` particles, which
        its rounding often raises by one, and stops releasing when it reaches
        ``MAXPAR``. A set ``maxpar`` is used as given. Unset, one location
        gets ``numpar`` (HYSPLIT may ask for one more and keeps ``numpar``),
        and several get ``locations * (numpar // locations + 1)``, so the
        last locations are not left short.

        Examples
        --------
        >>> HysplitConfig(n_hours=-24, numpar=1000).maxpar_for(40)
        1040
        >>> HysplitConfig(n_hours=-24, numpar=1000).maxpar_for(1)
        1000
        """
        if self.maxpar is not None:
            return self.maxpar
        if locations <= 1:
            return self.numpar
        return locations * (self.numpar // locations + 1)

    def hourly_ziscale(self) -> list[float]:
        """Return ``ziscale`` as one factor per hour: a single value repeated over the run, or the list as given."""
        if isinstance(self.ziscale, int | float):
            return [float(self.ziscale)] * max(abs(self.n_hours), 1)
        return [float(v) for v in self.ziscale]

    def realizations(self, n: int) -> list[Self]:
        """
        Return *n* realizations of this config, realization ``k`` with ``seed + k``.

        Realization 0 uses the configured seed.

        Raises
        ------
        ValueError
            If *n* is more than one and the runs would repeat one another:
            only ``krand=4`` (HYSPLIT seeds each run from the clock) and
            ``krand=2`` with a seed give each realization its own draws.
        """
        if n > 1 and not (
            self.krand == 4 or (self.krand == 2 and self.seed is not None)
        ):
            raise ValueError(
                f"realizations={n} requires krand=4 or krand=2 with a seed (got "
                f"krand={self.krand}, seed={self.seed}): under krand=4 HYSPLIT "
                "seeds each run from the clock; under krand=2 runtraj gives each "
                "realization its own seed. Any other mode would repeat the same "
                "perturbation."
            )
        return super().realizations(n)


__all__ = [
    "CONCENTRATION_SETTINGS",
    "CPACK_READ",
    "FIXED_CONCENTRATION_SETUP",
    "FIXED_SETUP",
    "PARTICLES_SETTINGS",
    "SAMPLING_TYPES",
    "ConcentrationGrid",
    "Pollutant",
    "HYSPLIT_VARSIWANT",
    "MAX_ZISCALE_HOURS",
    "WIND_ERROR_SETTINGS",
    "ZI_ERROR_SETTINGS",
    "HysplitConfig",
]

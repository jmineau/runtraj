"""The HYSPLIT ``CONTROL`` file."""

import datetime as dt
from pathlib import Path

from pydantic import BaseModel, Field

from runtraj.models.hysplit.config import (
    SAMPLING_TYPES,
    ConcentrationGrid,
    Pollutant,
)

#: The start of each grid's file name in a concentration run: ``cdump_<grid name>``.
CDUMP_PREFIX = "cdump_"

#: A time in ``CONTROL`` written as zeros: the start of the run (or, for a
#: sampling stop, its end).
_ZERO_TIME = "00 00 00 00 00"


def cdump_name(grid: str) -> str:
    """Return the file ``hycs_std`` writes the grid called *grid* to."""
    return f"{CDUMP_PREFIX}{grid}"


class ControlFile(BaseModel):
    """
    Contents of a HYSPLIT ``CONTROL`` file for one run of ``hycs_std``.

    Holds the release time and the release locations, the run time, the
    vertical motion option, the model top, and the met files, then the
    pollutant, concentration grid, and deposition sections. Without
    *pollutants* and *grids*, as in the particles mode, those sections are
    written with fixed values that the particle output does not use.

    Attributes
    ----------
    release_time : datetime
        The release time, in UTC, written to the minute.
    locations : list of tuple of float
        The release locations, each ``(longitude, latitude, height)`` in
        degrees and meters, in the order ``CONTROL`` lists them. The file
        writes each latitude first, as HYSPLIT reads it. Whether the
        heights are above ground or above sea level is ``SETUP.CFG``'s
        ``KMSL``, not in this file.
    emisshrs : float
        Hours of emission.
    n_hours : int
        Total run time, in hours, negative for a backward run.
    w_option : int
        Vertical motion option.
    z_top : float
        Top of the model domain, in meters above ground.
    met_files : list of Path
        The meteorology files, in the order HYSPLIT reads them.
    pollutants : list of Pollutant
        The pollutants of a concentration run, with their deposition.
        Empty for the fixed sections.
    grids : list of ConcentrationGrid
        The grids of a concentration run, each written to
        ``cdump_<name>`` in the workdir (:func:`cdump_name`). Empty for
        the fixed sections.
    """

    release_time: dt.datetime
    locations: list[tuple[float, float, float]]
    emisshrs: float
    n_hours: int
    w_option: int
    z_top: float
    met_files: list[Path]
    pollutants: list[Pollutant] = Field(default_factory=list)
    grids: list[ConcentrationGrid] = Field(default_factory=list)

    def write(self, path: str | Path) -> None:
        """
        Write the ``CONTROL`` file.

        Parameters
        ----------
        path : str or Path
            File to write.
        """
        lines: list[str] = []

        # (1) release time, with a 2-digit year (HYSPLIT convention)
        lines.append(self.release_time.strftime("%y %m %d %H %M"))

        # (2) number of release locations, (3) one line for each
        lines.append(str(len(self.locations)))
        for lon, lat, height in self.locations:
            lines.append(f"{lat} {lon} {height}")

        # Run parameters
        lines.append(str(self.n_hours))  # (4) total run time (hours)
        lines.append(str(self.w_option))  # (5) vertical motion option
        lines.append(
            str(self.z_top)
        )  # (6) top of model domain (internal coordinates, m-agl)

        # Met files - directory and filename on separate lines each. (7) is
        # "1 N": one grid with N files in time, at most 128 (mtim in
        # HYSPLIT's DEFGRID.INC). A bare "N" makes each file its own grid:
        # at most 12 in NOAA's builds (mgrd). Both give the same particles.
        lines.append(f"1 {len(self.met_files)}")
        for met_file in self.met_files:
            lines.append(
                str(met_file.parent) + "/"
            )  # (8) met file directory (trailing slash important for HYSPLIT)
            lines.append(met_file.name)  # (9) met file name

        if self.pollutants or self.grids:
            lines += _concentration_lines(self.pollutants, self.grids)
            Path(path).write_text("\n".join(lines) + "\n")
            return

        # Concentration / Pollutant Definition
        lines.append("1")  # (10) number of different pollutants
        lines.append("test")  # (11) pollutant 4-char identifier
        lines.append("1")  # (12) emission rate (per hour)
        lines.append(str(self.emisshrs))  # (13) hours of emission (can be fractional)
        # (14) release start (YY MM DD HH MM). Zeros mean the simulation
        # start, which keeps the release time's minute.
        lines.append("00 00 00 00 00")

        # Concentration / Grid  Definition (hardcoded - required by HYSPLIT's parser, not used by the particle output)
        lines.append("1")  # (15) number of simultaneous concentration grids
        lines.append(
            "0.0 0.0"
        )  # (16) grid center (lat, lon), 0s for the release location
        lines.append("0.5 0.5")  # (17) grid spacing (degrees)
        lines.append("30.0 30.0")  # (18) grid span (degrees)
        lines.append("./")  # (19) output directory
        lines.append(
            "cdump"
        )  # (20) output filename (the particles are in their own file)
        lines.append("1")  # (21) number of vertical concentration levels
        lines.append("100")  # (22) height of each level (m)
        lines.append("00 00 00 00 00")  # (23) sampling start
        lines.append("00 00 00 00 00")  # (24) sampling stop
        lines.append("00 2 00")  # (25) sampling interval (type 2 = maximum)

        # Concentration / Deposition Definition (all zeros = passive tracer gas, no deposition)
        lines.append("1")  # (26) number of pollutants depositing
        lines.append(
            "0.0 0.0 0.0"
        )  # (27) particle: diameter (um), density (g/cm3), shape
        lines.append(
            "0.0 0.0 0.0 0.0 0.0"
        )  # (28) deposition velocity (m/s), pollutant molecular weight (g/mol), surface reactivity ratio, diffusivity ratio, effective Henry's constant
        lines.append(
            "0.0 0.0 0.0"
        )  # (29) wet removal: actual Henry's constant, in-cloud, below-cloud (1/s)
        lines.append("0.0")  # (30) radioactive decay half-life (days)
        lines.append("0.0")  # (31) pollutant resuspension (1/m)

        Path(path).write_text("\n".join(lines) + "\n")

    @classmethod
    def read(cls, path: str | Path) -> "ControlFile":
        """
        Read a ``CONTROL`` file written by :meth:`write`.

        Parameters
        ----------
        path : str or Path
            File to read.

        Returns
        -------
        ControlFile
        """
        path = Path(path).resolve()

        if not path.exists():
            raise FileNotFoundError(
                "CONTROL file not found. Has the simulation been ran?"
            )

        with path.open() as f:
            lines = f.readlines()

        # Parse the release time
        release_time = dt.datetime.strptime(lines[0].strip(), "%y %m %d %H %M")

        # Parse the release locations
        n_locations = int(lines[1].strip())
        cursor = 2
        locations = []
        for i in range(cursor, cursor + n_locations):
            lat, lon, height = map(float, lines[i].strip().split())
            locations.append((lon, lat, height))

        cursor += n_locations
        n_hours = int(lines[cursor].strip())
        w_option = int(lines[cursor + 1].strip())
        z_top = float(lines[cursor + 2].strip())

        # Parse met files: "N" (one file per grid) or "grids files"
        counts = [int(value) for value in lines[cursor + 3].split()]
        n_met_files = counts[0] * (counts[1] if len(counts) > 1 else 1)
        cursor += 4
        met_files = []
        for i in range(n_met_files):
            dir_index = cursor + (i * 2)
            file_index = dir_index + 1
            met_file = Path(lines[dir_index].strip()) / lines[file_index].strip()
            met_files.append(met_file)

        cursor += 2 * n_met_files
        # Pollutant section: n_pollutant (0), species name (1), emission count (2),
        # then emisshrs at offset 3
        emisshrs = float(lines[cursor + 3].strip())
        pollutants, grids = _read_concentration(lines, cursor)

        return cls(
            release_time=release_time,
            locations=locations,
            emisshrs=emisshrs,
            n_hours=n_hours,
            w_option=w_option,
            z_top=z_top,
            met_files=met_files,
            pollutants=pollutants,
            grids=grids,
        )


def _concentration_lines(
    pollutants: list[Pollutant], grids: list[ConcentrationGrid]
) -> list[str]:
    """Return the pollutant, grid, and deposition lines of a concentration run's ``CONTROL``."""
    lines = [str(len(pollutants))]  # (10) number of pollutants
    for p in pollutants:
        lines += [
            p.name,  # (11) identifier
            repr(p.rate),  # (12) emission rate, per hour
            repr(p.hours),  # (13) hours of emission
            _ZERO_TIME,  # (14) release start: the start of the run
        ]
    lines.append(str(len(grids)))  # (15) number of grids
    for g in grids:
        center = (
            "0.0 0.0"  # the release location
            if g.center_lat is None
            else f"{g.center_lat!r} {g.center_lon!r}"
        )
        hours, minutes = divmod(g.sampling_interval, 60)
        lines += [
            center,  # (16) center, latitude first
            f"{g.spacing_lat!r} {g.spacing_lon!r}",  # (17) spacing, degrees
            f"{g.span_lat!r} {g.span_lon!r}",  # (18) span, degrees
            "./",  # (19) output directory
            cdump_name(g.name),  # (20) output file
            str(len(g.levels)),  # (21) number of levels
            " ".join(str(z) for z in g.levels),  # (22) the tops of the levels, m
            _ZERO_TIME,  # (23) sampling start: the start of the run
            _ZERO_TIME,  # (24) sampling stop: the end of the run
            f"{SAMPLING_TYPES[g.sampling]:02d} {hours:02d} {minutes:02d}",  # (25) type hh mm
        ]
    lines.append(str(len(pollutants)))  # (26) number of pollutants depositing
    for p in pollutants:
        lines += [
            f"{p.particle_diameter!r} {p.particle_density!r} {p.particle_shape!r}",  # (27)
            # (28) deposition velocity, molecular weight, surface reactivity,
            # diffusivity ratio, effective Henry's constant
            f"{p.deposition_velocity!r} {p.molecular_weight!r} "
            f"{p.surface_reactivity!r} {p.diffusivity_ratio!r} {p.henry_effective!r}",
            f"{p.henry!r} {p.in_cloud!r} {p.below_cloud!r}",  # (29) wet removal
            repr(p.half_life),  # (30) radioactive half-life, days
            repr(p.resuspension),  # (31) resuspension factor, 1/m
        ]
    return lines


def _read_concentration(
    lines: list[str], cursor: int
) -> tuple[list[Pollutant], list[ConcentrationGrid]]:
    """
    Return the pollutants and grids of the lines from *cursor*, the pollutant count.

    Both are empty for the fixed sections of the particles mode, whose one
    grid is written to ``cdump``.
    """
    words = [line.split() for line in lines]
    n_pol = int(words[cursor][0])
    starts = [cursor + 1 + 4 * k for k in range(n_pol)]
    at = cursor + 1 + 4 * n_pol
    n_grid = int(words[at][0])
    grid_at = [at + 1 + 10 * k for k in range(n_grid)]
    files = [lines[g + 4].strip() for g in grid_at]
    if not all(f.startswith(CDUMP_PREFIX) for f in files):
        return [], []
    grids = []
    for g, file in zip(grid_at, files, strict=True):
        lat, lon = map(float, words[g])
        kind, hh, mm = map(int, words[g + 9])
        grids.append(
            ConcentrationGrid(
                name=file[len(CDUMP_PREFIX) :],
                center_lon=None if (lat, lon) == (0.0, 0.0) else lon,
                center_lat=None if (lat, lon) == (0.0, 0.0) else lat,
                spacing_lat=float(words[g + 1][0]),
                spacing_lon=float(words[g + 1][1]),
                span_lat=float(words[g + 2][0]),
                span_lon=float(words[g + 2][1]),
                levels=[int(z) for z in words[g + 6]],
                sampling={v: k for k, v in SAMPLING_TYPES.items()}[kind],
                # HYSPLIT drops the minutes of an interval of an hour or more.
                sampling_interval=60 * hh if hh else mm,
            )
        )
    deposition = at + 1 + 10 * n_grid + 1
    pollutants = []
    for k, p in enumerate(starts):
        d = [words[deposition + 5 * k + i] for i in range(5)]
        pollutants.append(
            Pollutant(
                name=lines[p].strip(),
                rate=float(words[p + 1][0]),
                hours=float(words[p + 2][0]),
                particle_diameter=float(d[0][0]),
                particle_density=float(d[0][1]),
                particle_shape=float(d[0][2]),
                deposition_velocity=float(d[1][0]),
                molecular_weight=float(d[1][1]),
                surface_reactivity=float(d[1][2]),
                diffusivity_ratio=float(d[1][3]),
                henry_effective=float(d[1][4]),
                henry=float(d[2][0]),
                in_cloud=float(d[2][1]),
                below_cloud=float(d[2][2]),
                half_life=float(d[3][0]),
                resuspension=float(d[4][0]),
            )
        )
    return pollutants, grids

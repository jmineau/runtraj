"""
What HYSPLIT does with a release that another transport model may not: its line sources and its particle numbering.

:func:`check_releases` lists the multipoint releases HYSPLIT would turn
into a line source, and :func:`release_rows` says where each particle
started when HYSPLIT wrote no row at ``age = 0``, as HYSPLIT 5.1.0 to
5.4.2 do not. :class:`~runtraj.models.hysplit.HysplitModel` gives both to runtraj's
core (:class:`runtraj.models.TransportModel`), which refuses such a release
before HYSPLIT runs; :func:`off_the_hour` lists the releases that
``hyts_std`` before 5.4.0 cannot run.
"""

from __future__ import annotations

import math
import warnings
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from runtraj.particles.table import (
    _MIN_RELIABLE_SPACING_M,
    _closest_points_m,
    _height,
    column_release_rows,
)
from runtraj.releases import ColumnRelease, MultiPointRelease, Release

if TYPE_CHECKING:
    from .config import HysplitConfig

#: Why HYSPLIT cannot run a multipoint release whose points share a horizontal location.
REPEATED_LOCATION = (
    "HYSPLIT cannot run a MultiPointRelease whose points share a horizontal "
    "location: it collapses release locations that share a lat/lon into a "
    "single vertical line source and releases only between the last two "
    "heights. Use ColumnRelease for a vertical column, or one PointRelease "
    "per height (distinct group) for discrete release heights at one location."
)


#: Why HYSPLIT's trajectory program cannot run a column release.
COLUMN_TRAJECTORY = (
    "HYSPLIT's trajectory mode cannot run a ColumnRelease: a column is a "
    "line source of particles, and hyts_std follows one trajectory from "
    "each location. Use a PointRelease at each height (distinct group), or "
    "a MultiPointRelease."
)


#: Why a trajectory release off the hour is refused before HYSPLIT 5.4.0.
TRAJECTORY_OFF_HOUR = (
    "Before HYSPLIT 5.4.0, hyts_std ends the first hour of a run that starts "
    "off the hour at the next clock hour, so the run comes up short of "
    "n_hours and its rows fall on clock times instead of every tout from the "
    "release. Release on the hour, or use HYSPLIT 5.4.0 or later."
)


#: Why a run that counts each release location apart cannot take a column release.
COLUMN_SOURCES = (
    "HYSPLIT's ichem: 1 gives each release location a grid of its own, and "
    "a ColumnRelease is one line source between two locations: HYSPLIT "
    "leaves the grid of the first empty and puts the column in the second. "
    "Use a MultiPointRelease, whose points are the sources, or another ichem."
)


def check_releases(
    releases: pd.DataFrame, trajectory: bool = False, sources: bool = False
) -> pd.DataFrame:
    """
    Return the releases HYSPLIT cannot run, one row each, with ``check``, ``release`` and ``message``.

    *releases* has one row per release point (``release``, ``kind``,
    ``longitude``, ``latitude``). A multipoint release is refused when two
    of its points share a horizontal location: HYSPLIT joins consecutive
    release locations at one latitude and longitude, to 5 decimal places,
    into a vertical line source and releases only from the last pair.

    With *trajectory*, for ``hyts_std``, a column release is refused
    instead: every location starts a trajectory of its own, so a
    multipoint release may repeat a location, and there is no line source
    for a column.

    With *sources*, for a concentration run with ``ichem: 1``, a column
    release is refused as well as a multipoint one that repeats a
    location: the run's grids are one per release location, and a column
    is two locations and one source.
    """
    if trajectory:
        columns = pd.unique(releases.loc[releases["kind"] == "column", "release"])
        return pd.DataFrame(
            {
                "check": "release_refused",
                "release": columns,
                "message": COLUMN_TRAJECTORY,
            },
            columns=["check", "release", "message"],
        )
    multi = releases[releases["kind"] == "multipoint"]
    repeated = _repeated(multi["release"], multi["longitude"], multi["latitude"])
    bad = pd.unique(multi["release"].to_numpy()[repeated])
    refused = pd.DataFrame(
        {"check": "release_refused", "release": bad, "message": REPEATED_LOCATION},
        columns=["check", "release", "message"],
    )
    if not sources:
        return refused
    columns = pd.unique(releases.loc[releases["kind"] == "column", "release"])
    return pd.concat(
        [
            refused,
            pd.DataFrame(
                {
                    "check": "release_refused",
                    "release": columns,
                    "message": COLUMN_SOURCES,
                },
                columns=["check", "release", "message"],
            ),
        ],
        ignore_index=True,
    )


def off_the_hour(releases: pd.DataFrame) -> pd.DataFrame:
    """
    Return the releases of *releases* whose time is not on the hour, one row each, with ``check``, ``release`` and ``message``.

    *releases* has a ``release`` and a ``time`` column. The message says why
    ``hyts_std`` before HYSPLIT 5.4.0 cannot run them (:data:`TRAJECTORY_OFF_HOUR`).
    """
    off = releases.loc[releases["time"].dt.minute != 0, "release"]
    return pd.DataFrame(
        {
            "check": "release_refused",
            "release": pd.unique(off),
            "message": TRAJECTORY_OFF_HOUR,
        },
        columns=["check", "release", "message"],
    )


def _repeated(release: object, lon: object, lat: object) -> np.ndarray:
    """Return which points share a horizontal location, to 5 decimal places, with an earlier point of their release."""
    points = pd.DataFrame(
        {
            "release": np.asarray(release),
            "lon": np.round(np.asarray(lon, dtype=float), 5),
            "lat": np.round(np.asarray(lat, dtype=float), 5),
        }
    )
    return points.duplicated().to_numpy()


def release_rows(
    particles: pd.DataFrame, release: Release, config: HysplitConfig
) -> pd.DataFrame:
    """
    Return where each particle of a HYSPLIT run started, for a run that wrote no row at ``age = 0``.

    The columns are :data:`runtraj.particles.table.RELEASE_ROW_COLUMNS`. A
    column's particles are released bottom to top in ``particle`` order,
    :func:`particles_per_step` of them at each one-minute time step of the
    release (``emisshrs``), each a column of its own: particle ``k`` starts
    in slab ``(k - 1) % n + 1`` of the ``n`` released with it. So a start
    height is known to the depth of those slabs, ``(top - bottom) / n``:
    a long release of few particles knows it less well than a short one. A
    multipoint release's particles are matched to a point from their first
    row: by height when every point has its own altitude, since height
    drifts about 30 times less than horizontal position over the first
    step, and otherwise by position, with a warning when the points are too
    close for that.
    """
    if isinstance(release, ColumnRelease):
        # A column's particle numbers run to the highest in *particles*, even
        # when a particle left the domain before its first row.
        return column_release_rows(
            release, int(particles["particle"].max()), particles_per_step(config)
        )
    if isinstance(release, MultiPointRelease):
        return _multipoint_rows(particles, release)
    raise ValueError(f"A {release.kind} release needs no release rows.")


def particles_per_step(config: HysplitConfig, locations: int = 1) -> int:
    """
    Return how many particles HYSPLIT releases from one location in one block of a time step of a particle run.

    HYSPLIT's ``EMSPNT`` spreads ``numpar``, divided among the *locations*
    horizontal locations of the release, over the ``emisshrs`` of the
    release, rounding up the particles of an hour and then of a step, in
    single precision. A particle run (``ICHEM=8``) steps one minute, so a
    release of a minute or less is one step of ``numpar`` or ``numpar + 1``
    particles; a longer one is a step every minute until ``MAXPAR``
    particles are out, the last step cut short. Each step releases a block
    of this many particles for each of the ``nbptyp`` size bins, and with
    ``maxdim: 1`` the bins share ``numpar``. A puff run (``initd`` 1 or
    2, or a three-digit ``initd`` whose tens digit is 1 or 2) releases one
    puff a step, at the middle of the column. HYSPLIT 5.1.0 to 5.4.2 count the same way.

    Examples
    --------
    >>> from runtraj.models.hysplit import HysplitConfig
    >>> particles_per_step(HysplitConfig(n_hours=-24, numpar=1000))
    1001
    >>> particles_per_step(HysplitConfig(n_hours=-24, numpar=1000, emisshrs=0.5))
    34
    >>> particles_per_step(HysplitConfig(n_hours=-24, numpar=1000, emisshrs=0.5), 3)
    12
    """
    initial = config.initd if config.initd < 100 else config.initd // 10 % 10
    if initial in (1, 2):
        return 1
    minute = np.float32(1.0) / np.float32(60.0)  # ABS(DT/60.0)
    hours = max(minute, np.float32(config.emisshrs))
    qsum = hours
    if config.maxdim == 1:  # HYSPLIT sums the hours of the bins
        for _ in range(config.nbptyp - 1):
            qsum = qsum + hours
    per_hour = math.ceil(np.float32(config.numpar // locations) / qsum)
    return max(1, math.ceil(np.float32(per_hour) * minute))


def _multipoint_rows(p: pd.DataFrame, release: MultiPointRelease) -> pd.DataFrame:
    """Return each particle's start: the point its first row is matched to."""
    first = (
        p.assign(_age=p["age"].abs())
        .sort_values("_age", kind="stable")
        .drop_duplicates(subset="particle")
    )
    lons = np.asarray(release.longitudes, dtype=float)
    lats = np.asarray(release.latitudes, dtype=float)
    alts = np.asarray(release.altitudes, dtype=float)
    has_t0 = bool((first["_age"] == 0).all())
    height = _height(first, release)

    if not has_t0 and not np.isnan(height).any() and len(np.unique(alts)) == len(alts):
        nearest = np.argmin(np.abs(np.asarray(height)[:, None] - alts[None, :]), axis=1)
    else:
        xy = first[["lon", "lat"]].to_numpy(dtype=float)
        pts = np.column_stack((lons, lats))
        nearest = np.argmin(
            np.sum((xy[:, None, :] - pts[None, :, :]) ** 2, axis=2), axis=1
        )
        if not has_t0 and len(alts) > 1:
            spacing = _closest_points_m(lons, lats)
            if spacing < _MIN_RELIABLE_SPACING_M:
                warnings.warn(
                    f"MultiPointRelease release points are as close as "
                    f"{spacing:.0f} m and cannot be separated by altitude, and this "
                    "HYSPLIT build writes no t=0 row, so particles cannot be "
                    "reliably matched to their release points; 'release_height' may be wrong. "
                    "Use a HYSPLIT build that writes release-time rows "
                    "(HysplitConfig.directory) or space the points more than "
                    f"{_MIN_RELIABLE_SPACING_M:.0f} m apart.",
                    stacklevel=4,
                )
    return pd.DataFrame(
        {
            "particle": first["particle"].to_numpy(),
            "lon": lons[nearest],
            "lat": lats[nearest],
            "height": alts[nearest],
        }
    )

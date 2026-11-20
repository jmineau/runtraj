"""
The checks a release must pass, each written once for many points.

Each check returns ``(bad, message)``: the points that fail it and why. A
release runs the checks on its own points and raises the first failure
(:mod:`runtraj.releases.models`). The release table runs them on a whole
file and names the first bad row (:func:`runtraj.releases.table.release_rows`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import numpy as np


def point_errors(
    lon: Any, lat: Any, alt: Any, altitude_ref: Any
) -> list[tuple[np.ndarray, str]]:
    """
    Return the checks every release point must pass, as ``(bad, message)``.

    *lon*, *lat*, and *alt* hold one value per point; *altitude_ref* holds
    one per point or one for all.
    """
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    alt = np.asarray(alt, dtype=float)
    agl = np.asarray(altitude_ref) == "agl"
    # Written as "not inside", so a NaN, which compares False, fails too.
    return [
        (
            ~((lon >= -180) & (lon <= 180)),
            "longitude must be a number within [-180, 180].",
        ),
        (~((lat >= -90) & (lat <= 90)), "latitude must be a number within [-90, 90]."),
        (~np.isfinite(alt), "altitude must be a finite number."),
        (agl & (alt < 0), "AGL altitudes must be >= 0."),
    ]


def column_errors(bottom: Any, top: Any) -> list[tuple[np.ndarray, str]]:
    """Return the check a column must pass, as ``(bad, message)``: bottom below top."""
    bad = np.asarray(bottom, dtype=float) >= np.asarray(top, dtype=float)
    return [(bad, "'bottom' must be less than 'top'.")]


def two_points_errors(lon: Any, lat: Any) -> list[tuple[np.ndarray, str]]:
    """
    Return the check a multipoint release of two points must pass, as ``(bad, message)``.

    In ``releases.csv`` a group of two rows at exactly one horizontal
    location is a column release, so a multipoint release of two such
    points would be read back as a column. The comparison is exact, as the
    file's is.
    """
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    bad = len(lon) == 2 and lon[0] == lon[1] and lat[0] == lat[1]
    return [
        (
            np.array([bad]),
            "A MultiPointRelease of two points at one location is a "
            "ColumnRelease in releases.csv, which would read it back as one. "
            "Use ColumnRelease for a column, or add a third point.",
        )
    ]


def _raise_first(errors: Iterable[tuple[np.ndarray, str]]) -> None:
    """Raise the message of the first check any point fails."""
    for bad, message in errors:
        if bad.any():
            raise ValueError(message)

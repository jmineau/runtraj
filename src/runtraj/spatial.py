"""
Boxes and coordinate systems: a longitude/latitude box, and CRS helpers.

:class:`Bounds` is a longitude/latitude box, such as the crop box of a met
or the domain whose edge :meth:`~runtraj.particles.ParticlesAccessor.grid_exits`
finds. This module does not need shapely, so any part of runtraj can use it.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Self

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    import xarray as xr
    from numpy.typing import ArrayLike

# ---------------------------------------------------------------------------
# CF attributes
# ---------------------------------------------------------------------------


def cf_axis_attrs(dim: str) -> dict[str, str]:
    """
    Return the CF attributes for one coordinate axis: ``lon``, ``lat``, ``x`` or ``y``.

    Arrays on a raster use this, so their axes carry the same attributes
    wherever they are made.
    """
    return {
        "lon": {
            "standard_name": "longitude",
            "long_name": "longitude",
            "units": "degrees_east",
            "axis": "X",
        },
        "lat": {
            "standard_name": "latitude",
            "long_name": "latitude",
            "units": "degrees_north",
            "axis": "Y",
        },
        "x": {
            "standard_name": "projection_x_coordinate",
            "long_name": "x coordinate of projection",
            "units": "m",
            "axis": "X",
        },
        "y": {
            "standard_name": "projection_y_coordinate",
            "long_name": "y coordinate of projection",
            "units": "m",
            "axis": "Y",
        },
    }[dim]


_HORIZONTAL_DIMS = (("lat", "lon"), ("y", "x"))


def horizontal_dims(data: xr.DataArray) -> tuple[str, str]:
    """
    Return the names of the horizontal dimensions, ``(y_dim, x_dim)``.

    Raises
    ------
    ValueError
        If *data* has neither ``lat``/``lon`` nor ``y``/``x`` dimensions.
    """
    for y_dim, x_dim in _HORIZONTAL_DIMS:
        if y_dim in data.dims and x_dim in data.dims:
            return y_dim, x_dim
    raise ValueError(
        f"Expected 'lat'/'lon' or 'y'/'x' dimensions; got {tuple(data.dims)}."
    )


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


class Bounds(BaseModel):
    """
    Longitude/latitude bounding box, in degrees.

    The edges are finite numbers, and ``-90 <= ymin < ymax <= 90``.
    Longitudes count modulo 360 (:meth:`contains`).
    """

    model_config = ConfigDict(frozen=True)

    xmin: float = Field(..., description="Western edge, in degrees longitude.")
    xmax: float = Field(..., description="Eastern edge, in degrees longitude.")
    ymin: float = Field(..., description="Southern edge, in degrees latitude.")
    ymax: float = Field(..., description="Northern edge, in degrees latitude.")

    @model_validator(mode="after")
    def _on_the_globe(self) -> Self:
        """Refuse an edge that is not a finite number, and latitudes off the globe or out of order, which no point is inside."""
        edges = {
            "xmin": self.xmin,
            "xmax": self.xmax,
            "ymin": self.ymin,
            "ymax": self.ymax,
        }
        if not np.isfinite(list(edges.values())).all():
            got = ", ".join(f"{name}={value:g}" for name, value in edges.items())
            raise ValueError(f"Bounds need four finite numbers; got {got}.")
        if not -90.0 <= self.ymin < self.ymax <= 90.0:
            raise ValueError(
                f"Bounds need -90 <= ymin < ymax <= 90 degrees latitude; got "
                f"ymin={self.ymin:g}, ymax={self.ymax:g}."
            )
        return self

    def contains(self, longitude: ArrayLike, latitude: ArrayLike) -> np.ndarray:
        """
        Return whether each point is in the box, counting longitudes modulo 360.

        A longitude is in the box when, counted east from ``xmin``, it is
        within the box's width, so a box may cross the dateline (``xmin=170``,
        ``xmax=-170``) or reach past 180 (``xmax=190``), and ``-175`` and
        ``185`` are the same meridian. A box 360 degrees wide or more holds
        every longitude. Points on an edge are inside; the longitude edges
        allow ``1e-9`` degree for rounding, since a sum such as
        ``184.7 + 175.3`` need not give exactly 360. A point with a
        missing coordinate is not in the box.

        Parameters
        ----------
        longitude, latitude : array_like of float
            Degrees. Scalars or arrays, broadcast against each other.

        Returns
        -------
        numpy.ndarray of bool
            One value per point (a 0-d array for scalars).

        Examples
        --------
        >>> import runtraj
        >>> utah = runtraj.Bounds(xmin=-114, xmax=-109, ymin=37, ymax=42)
        >>> bool(utah.contains(-111.9, 40.8))
        True
        >>> bool(utah.contains(-111.9, 45.0))
        False

        A box may cross the dateline, and ``-175`` and ``185`` are one meridian:

        >>> pacific = runtraj.Bounds(xmin=170, xmax=-170, ymin=-10, ymax=10)
        >>> pacific.contains([175, -175, 185, 0], 0).tolist()
        [True, True, True, False]
        """
        lon = np.asarray(longitude, dtype=float)
        lat = np.asarray(latitude, dtype=float)
        in_latitude = (lat >= self.ymin) & (lat <= self.ymax)
        if self.xmax - self.xmin >= 360.0:
            in_longitude = ~np.isnan(lon)
        else:
            tolerance = 1e-9
            width = (self.xmax - self.xmin) % 360.0
            from_west = (lon - self.xmin) % 360.0
            in_longitude = (from_west <= width + tolerance) | (
                from_west >= 360.0 - tolerance
            )
        return np.asarray(in_longitude & in_latitude)


# ---------------------------------------------------------------------------
# CRS helpers
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=64)
def is_longlat(crs: str) -> bool:
    """
    Return whether ``crs`` is geographic (longitude/latitude degrees).

    This is the one test runtraj uses, for grids, meshes, and plots alike.
    PROJ strings are read by eye; other forms, such as ``EPSG:4326``, by
    pyproj.
    """
    if "+proj=longlat" in crs:
        return True
    if crs.upper() in {"EPSG:4326", "WGS84", "OGC:CRS84"}:
        return True
    if crs.startswith("+"):
        return False
    try:
        from pyproj import CRS

        return bool(CRS.from_user_input(crs).is_geographic)
    except Exception:  # not a CRS pyproj can read
        return False


def same_crs(a: str, b: str) -> bool:
    """Return whether two CRS strings describe the same CRS, however they are spelled."""
    if a == b:
        return True
    if is_longlat(a) and is_longlat(b):
        return True
    try:
        from pyproj import CRS

        return CRS.from_user_input(a) == CRS.from_user_input(b)
    except Exception:  # not a CRS pyproj can read
        return False


def haversine_km(
    lon1: np.ndarray | float,
    lat1: np.ndarray | float,
    lon2: np.ndarray | float,
    lat2: np.ndarray | float,
) -> np.ndarray:
    """Return the great-circle distance in km, broadcasting over the inputs."""
    d_lat = np.radians(lat2 - lat1)
    d_lon = np.radians(lon2 - lon1)
    a = (
        np.sin(d_lat / 2) ** 2
        + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(d_lon / 2) ** 2
    )
    return 6371.0 * 2 * np.arcsin(np.sqrt(a))


__all__ = [
    "Bounds",
    "cf_axis_attrs",
    "haversine_km",
    "horizontal_dims",
    "is_longlat",
    "same_crs",
]

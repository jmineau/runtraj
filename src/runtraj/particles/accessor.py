"""
The ``.runtraj`` accessor on a particle table: endpoints, grid exits, export, plots.

Importing :mod:`runtraj` registers :class:`ParticlesAccessor` as
``particles.runtraj`` on every pandas DataFrame.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import xarray as xr

from runtraj.particles._cf import to_cf_trajectories

if TYPE_CHECKING:
    from runtraj.spatial import Bounds
    from runtraj.visualization import ParticlesPlotAccessor

#: The edges of a box, in the order of the columns of ``crossings`` in :meth:`ParticlesAccessor.grid_exits`.
_EDGES = np.array(["n", "e", "s", "w"])

#: Rounding allowed when a point lies on an edge, in degrees.
_EDGE_TOLERANCE = 1e-9


def _box_edges(bounds: Bounds) -> tuple[float, float, float, float]:
    """
    Return the edges of *bounds*, ``(west, east, south, north)``, in degrees.

    Longitudes are read as :meth:`~runtraj.Bounds.contains` reads them,
    counted east from ``xmin`` and not wrapped: a box from 170 to -170 has
    ``east`` 190, and a box 360 degrees wide or more goes all the way around.
    """
    span = bounds.xmax - bounds.xmin
    width = 360.0 if span >= 360.0 else span % 360.0
    return bounds.xmin, bounds.xmin + width, bounds.ymin, bounds.ymax


def _wrap_degrees(difference: np.ndarray) -> np.ndarray:
    """Return a difference of longitudes as the equivalent one in [-180, 180)."""
    return (difference + 180.0) % 360.0 - 180.0


@pd.api.extensions.register_dataframe_accessor("runtraj")
class ParticlesAccessor:
    """
    runtraj's methods on a particle table, as ``particles.runtraj``.

    A package can subclass it and register the subclass under its own
    name.

    Examples
    --------
    >>> particles = sim.particles  # doctest: +SKIP
    >>> particles.runtraj.endpoints()  # doctest: +SKIP
    >>> particles.runtraj.grid_exits(bounds)  # doctest: +SKIP
    >>> particles.runtraj.to_cf_trajectories()  # doctest: +SKIP
    >>> particles.runtraj.plot.map()  # doctest: +SKIP
    """

    def __init__(self, particles: pd.DataFrame) -> None:
        self._particles = particles

    def endpoints(self) -> pd.DataFrame:
        """
        Return the last row of each particle, the one farthest in time from release.

        For a backward run this is where the air came from, which is where
        to sample a background concentration field. For a forward run it is
        where the air went. A particle that left the meteorology domain
        early ends where it left.

        Returns
        -------
        pandas.DataFrame
            One row per particle, with every column of the particle table.
            ``age`` is minutes since release and ``time`` the UTC time.
        """
        p = self._particles.reset_index(drop=True)
        if p.empty:
            return p
        reach = p["age"].abs()
        last = reach.groupby(p["particle"], sort=False).idxmax().to_numpy(dtype=int)
        return p.iloc[last]

    def grid_exits(self, bounds: Bounds) -> pd.DataFrame:
        """
        Return where each particle first left a longitude/latitude box.

        For a backward run these are the points where the air entered the
        domain, where a field at the domain's edge is sampled. Unlike
        :meth:`endpoints`, which gives where a particle ended, this gives
        where it first crossed the edge of the box, even if it came back
        later or stayed in the meteorology domain long after.

        A particle is outside when it is beyond an edge of the box, in
        longitude and latitude. Longitudes are read as
        :meth:`~runtraj.Bounds.contains` reads them: a box across the
        dateline or past 180 degrees is read modulo 360, and a box 360
        degrees wide or more has no west or east edge. Rows are read in
        order of ``abs(age)``, whatever their order in the table, so this
        works for a backward and a forward run.

        The point is on the edge: the exit is the straight line between the
        particle's last row inside the box and its first row outside,
        and ``lon``, ``lat``, ``zagl``, ``age`` and ``time`` are interpolated
        along it to where it crosses. If the line crosses two edges at a
        corner, ``edge`` is the first it crosses. A particle whose first row is
        already outside has no row inside to interpolate from (the
        meteorology HYSPLIT 5.1.0 writes no row at
        the release), so it is reported at that row, on the edge it is
        farthest beyond.

        Parameters
        ----------
        bounds : runtraj.Bounds
            The box, such as the edge of your study area.

        Returns
        -------
        pandas.DataFrame
            One row per particle that left the box, ordered by ``particle``;
            a particle that stayed inside is not in it. The columns are
            ``particle``, ``edge`` (``"n"``, ``"e"``, ``"s"`` or ``"w"``,
            the side of the box it left by), ``lon`` and ``lat`` (degrees),
            ``zagl`` (meters), ``age`` (minutes since release) and, when the
            table has a ``time`` column, ``time`` (UTC). Longitudes are
            wrapped to -180 to 180 where the interpolation crosses 180.

        Examples
        --------
        One particle that leaves a box by its east edge, between two rows:

        >>> import pandas as pd
        >>> import runtraj
        >>> box = runtraj.Bounds(xmin=-112, xmax=-110, ymin=40, ymax=42)
        >>> particles = pd.DataFrame(
        ...     {
        ...         "particle": [1, 1, 1],
        ...         "age": [0.0, -60.0, -120.0],
        ...         "lon": [-111.0, -110.5, -109.5],
        ...         "lat": [41.0, 41.0, 41.0],
        ...         "zagl": [10.0, 20.0, 30.0],
        ...     }
        ... )
        >>> particles.runtraj.grid_exits(box)
           particle edge    lon   lat  zagl   age
        0         1    e -110.0  41.0  25.0 -90.0
        """
        p = self._particles
        p = p.iloc[np.lexsort((p["age"].abs().to_numpy(), p["particle"].to_numpy()))]
        p = p.reset_index(drop=True)
        particle = p["particle"].to_numpy()
        lon = p["lon"].to_numpy(dtype=float)
        lat = p["lat"].to_numpy(dtype=float)

        # Positions in degrees, measured from the box's center.
        west, east, south, north = _box_edges(bounds)
        x = _wrap_degrees(lon - (west + east) / 2)
        y = lat - (south + north) / 2
        half_width, half_height = (east - west) / 2, (north - south) / 2
        if east - west >= 360.0 - _EDGE_TOLERANCE:
            half_width = np.inf  # all the way around: no west or east edge

        # The first row of each particle that is beyond the edge of the box.
        outside = (np.abs(x) > half_width + _EDGE_TOLERANCE) | (
            np.abs(y) > half_height + _EDGE_TOLERANCE
        )
        rows = np.flatnonzero(outside)
        rows = rows[np.unique(particle[rows], return_index=True)[1]]

        # The row before it, inside the box; a particle that starts outside has none.
        before = np.maximum(rows - 1, 0)
        has_before = (rows > 0) & (particle[before] == particle[rows])
        before = np.where(has_before, before, rows)

        # Where the step from `before` to `rows` crosses each edge, as a fraction of it.
        step_x = _wrap_degrees(x[rows] - x[before])
        step_y = y[rows] - y[before]
        end_x, end_y = x[before] + step_x, y[before] + step_y
        with np.errstate(divide="ignore", invalid="ignore"):
            crossings = np.column_stack(
                [
                    np.where(
                        end_y > half_height, (half_height - y[before]) / step_y, np.inf
                    ),
                    np.where(
                        end_x > half_width, (half_width - x[before]) / step_x, np.inf
                    ),
                    np.where(
                        end_y < -half_height,
                        (-half_height - y[before]) / step_y,
                        np.inf,
                    ),
                    np.where(
                        end_x < -half_width, (-half_width - x[before]) / step_x, np.inf
                    ),
                ]
            )
            beyond = np.column_stack(  # how far past each edge, in degrees
                [
                    end_y - half_height,
                    end_x - half_width,
                    -half_height - end_y,
                    -half_width - end_x,
                ]
            )
        first = np.argmin(crossings, axis=1)
        farthest = np.argmax(beyond, axis=1)
        edge = np.where(has_before, first, farthest)
        fraction = np.where(has_before, crossings[np.arange(rows.size), first], 1.0)
        fraction = np.clip(fraction, 0.0, 1.0)

        def at_edge(column: str) -> np.ndarray:
            values = p[column].to_numpy(dtype=float)
            return values[before] + fraction * (values[rows] - values[before])

        step_lon = _wrap_degrees(lon[rows] - lon[before])
        exit_lon = lon[before] + fraction * step_lon
        exit_lon = np.where(np.abs(exit_lon) > 180.0, _wrap_degrees(exit_lon), exit_lon)
        exits = pd.DataFrame(
            {
                "particle": particle[rows],
                "edge": _EDGES[edge],
                "lon": exit_lon,
                "lat": at_edge("lat"),
                "zagl": at_edge("zagl"),
                "age": at_edge("age"),
            }
        )
        if "time" in p.columns:
            time_before = p["time"].iloc[before].reset_index(drop=True)
            time_after = p["time"].iloc[rows].reset_index(drop=True)
            exits["time"] = time_before + (time_after - time_before) * fraction
        return exits

    def to_cf_trajectories(self) -> xr.Dataset:
        """
        Return the particles as a CF-1.10 trajectory dataset, one trajectory per particle.

        The dataset is what trajan, cf-xarray and other tools that read CF
        trajectories open. Write it to NetCDF with
        :meth:`xarray.Dataset.to_netcdf`. Each particle is one trajectory
        along the ``trajectory`` dimension, and its rows, from its release
        onward, run along ``obs``. A particle with fewer rows than the
        longest (one that left the meteorology domain early) is padded with
        missing values at the end. ``time`` is UTC, and ``age``, in minutes
        since release, is kept as a variable. ``obs`` counts output steps
        from the release, so in a backward run ``time`` decreases along
        ``obs``.

        This is CF's "incomplete multidimensional array" representation of
        trajectories (CF-1.10, appendix H.4.1).

        Returns
        -------
        xarray.Dataset
            Dimensions ``(trajectory, obs)``. The coordinates are
            ``trajectory`` (the particle number), ``time``, ``lon``, ``lat``
            and ``zagl``. ``age`` and every other numeric column of the table
            are data variables, with units for the columns runtraj and
            HYSPLIT write.

        Raises
        ------
        ValueError
            If the table lacks one of ``particle``, ``age``, ``lon``, ``lat``
            ``zagl`` or ``time``, if a particle has two rows at the same age, or if
            it holds more than one realization (select one:
            ``particles[particles.realization == k]``).

        Examples
        --------
        >>> import pandas as pd
        >>> particles = pd.DataFrame(
        ...     {
        ...         "particle": [1, 1, 1, 2, 2],
        ...         "age": [-1.0, -2.0, -3.0, -1.0, -2.0],
        ...         "lon": [-111.85, -111.86, -111.88, -111.84, -111.83],
        ...         "lat": [40.77, 40.78, 40.80, 40.76, 40.75],
        ...         "zagl": [5.0, 40.0, 120.0, 8.0, 60.0],
        ...     }
        ... )
        >>> particles["time"] = pd.Timestamp("2024-01-15 18:00") + pd.to_timedelta(
        ...     particles["age"], unit="min"
        ... )
        >>> ds = particles.runtraj.to_cf_trajectories()
        >>> dict(ds.sizes)
        {'trajectory': 2, 'obs': 3}
        >>> ds.attrs["featureType"]
        'trajectory'
        >>> ds.age.sel(trajectory=2).values
        array([-1., -2., nan])
        >>> str(ds.time.sel(trajectory=1).values[-1])
        '2024-01-15T17:57:00.000000000'
        """
        return to_cf_trajectories(self._particles)

    @property
    def plot(self) -> ParticlesPlotAccessor:
        """Plotting methods, such as ``particles.runtraj.plot.map()``."""
        from runtraj.visualization import ParticlesPlotAccessor

        return ParticlesPlotAccessor(self._particles)

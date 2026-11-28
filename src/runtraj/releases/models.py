"""
The release models, their times, and their ids.

A :class:`PointRelease` releases from one point, a :class:`ColumnRelease`
from a vertical line, and a :class:`MultiPointRelease` from several points
at once (for example a slanted satellite sounding).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from functools import cached_property
from types import ModuleType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    TypeAlias,
    cast,
)

import numpy as np
import pandas as pd
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from shapely import Geometry, LineString, MultiPoint, Point

    from runtraj.visualization import ReleasePlotAccessor

from .validation import (
    _raise_first,
    column_errors,
    point_errors,
    two_points_errors,
)


def _shapely() -> ModuleType:
    """Import shapely, which only a release's geometry needs."""
    try:
        import shapely
    except ImportError as error:
        raise ImportError(
            "A release's geometry requires shapely, which the geometry extra "
            'installs: pip install "runtraj[geometry]"'
        ) from error
    return shapely


TimeLike: TypeAlias = dt.datetime | pd.Timestamp | np.datetime64 | str

#: Whether an altitude is above ground level or above mean sea level.
VerticalReference: TypeAlias = Literal["agl", "msl"]


# ---------------------------------------------------------------------------
# Times and ids
# ---------------------------------------------------------------------------


#: The forms a release time may take as text, for messages.
_TIME_FORMS = (
    "ISO 8601, such as '2023-07-15 18:00' or '2023-07-15T18:00:00Z', "
    "or YYYYMMDDHHMM, such as '202307151800'"
)


def _parse_time_text(text: pd.Series) -> pd.Series:
    """
    Read release times written as text, and return them as UTC times.

    Each value is ISO 8601 or ``YYYYMMDDHHMM``. A time with an offset is
    converted to UTC; one without is taken as UTC. A value in any other
    form gives ``NaT``, as does a missing one: the day-first
    ``05/07/2023 18:00`` could also be read month-first, and pandas' words
    ``now`` and ``today`` are no fixed time.
    """
    text = text.astype("string").str.strip()
    compact = text.str.fullmatch(r"\d{12}").fillna(False).to_numpy(dtype=bool)
    word = text.str.lower().isin(["now", "today"]).to_numpy(dtype=bool)
    times = pd.Series(pd.NaT, index=text.index, dtype="datetime64[ns, UTC]")
    if compact.any():
        times[compact] = pd.to_datetime(text[compact], format="%Y%m%d%H%M", utc=True)
    iso = ~compact & ~word
    if iso.any():
        times[iso] = pd.to_datetime(
            text[iso], format="ISO8601", utc=True, errors="coerce"
        )
    return times


def parse_time(time: TimeLike) -> dt.datetime:
    """
    Return *time* as a naive UTC datetime.

    Accepts datetimes (aware ones are converted to UTC), pandas and numpy
    times, and text in ISO 8601 or the compact ``"YYYYMMDDHHMM"`` form
    (:func:`_parse_time_text`). Other text, such as the day-first
    ``"05/07/2023 18:00"``, is refused, since it could be read either way.
    Seconds are dropped: a release starts on the minute, as the transport
    model is told and as the release id names it.
    """
    if time is None:
        raise ValueError("'time' must be provided for all release types.")
    if isinstance(time, (int, float, np.integer, np.floating)):
        raise TypeError(
            "Numeric release times are not accepted. Pass a datetime-like value "
            "or a string such as '202301011200' or '2023-01-01T12:00:00Z'."
        )
    if isinstance(time, str):
        parsed = pd.Timestamp(_parse_time_text(pd.Series([time])).iloc[0])
        if str(parsed) == "NaT":
            raise ValueError(f"Release time {time!r} is not {_TIME_FORMS}.")
    elif isinstance(time, (dt.datetime, pd.Timestamp, np.datetime64)):
        parsed = pd.Timestamp(time)
    else:
        raise TypeError(
            "Release time must be a datetime-like value or supported time string."
        )
    if str(parsed) == "NaT":
        raise ValueError("Release time cannot be NaT.")
    if parsed.tzinfo is not None:
        parsed = parsed.tz_convert("UTC").tz_localize(None)
    # HYSPLIT's CONTROL and the release id are to the minute, so the time is too.
    parsed = parsed.floor("min")
    return cast(dt.datetime, parsed.to_pydatetime()).replace(tzinfo=None)


def parse_release_id(release_id: str) -> tuple[dt.datetime, str]:
    """
    Split a release id into its release time and location id.

    An id is ``"<YYYYMMDDHHMM>_<location>"``, where the location is
    ``"<lon>_<lat>_<alt>"`` for a point, ``"<lon>_<lat>_X<bottom>-<top>"``
    for a column, and ``"multi_<hash>"`` for a multipoint release. Heights
    above mean sea level end the location with ``msl``.

    Raises
    ------
    ValueError
        If *release_id* does not start with a ``YYYYMMDDHHMM`` time and an
        underscore.
    """
    stamp, _, location = str(release_id).partition("_")
    try:
        if len(stamp) != 12 or not stamp.isdigit() or not location:
            raise ValueError
        time = dt.datetime.strptime(stamp, "%Y%m%d%H%M")
    except ValueError:
        raise ValueError(
            f"Release id {release_id!r} is not of the form "
            "'{YYYYMMDDHHMM}_{location_id}'."
        ) from None
    return time, location


def _format_coord(val: float) -> str:
    """Format a coordinate without a decimal point when it is a whole number."""
    return str(int(val)) if val == int(val) else str(val)


def _ref_suffix(altitude_ref: str) -> str:
    """Return the end of a location id for a vertical reference: ``"msl"`` or nothing."""
    return "msl" if altitude_ref == "msl" else ""


def _point_location(lon: float, lat: float, alt: float, altitude_ref: str) -> str:
    """Location id of a point release, ``"<lon>_<lat>_<alt>"`` (``msl`` appended above sea level)."""
    return (
        f"{_format_coord(lon)}_{_format_coord(lat)}_{_format_coord(alt)}"
        f"{_ref_suffix(altitude_ref)}"
    )


def _column_location(
    lon: float, lat: float, bottom: float, top: float, altitude_ref: str
) -> str:
    """Location id of a column release, ``"<lon>_<lat>_X<bottom>-<top>"`` (``msl`` appended above sea level)."""
    return (
        f"{_format_coord(lon)}_{_format_coord(lat)}"
        f"_X{_format_coord(bottom)}-{_format_coord(top)}{_ref_suffix(altitude_ref)}"
    )


def _multipoint_location(
    lons: Iterable[float],
    lats: Iterable[float],
    alts: Iterable[float],
    altitude_ref: str,
) -> str:
    """
    Location id of a multipoint release, ``"multi_<hash>"`` (``msl`` appended above sea level).

    The hash covers the sorted points, with longitudes and latitudes rounded
    to 5 decimals and altitudes to 0.01 m. A whole-meter altitude hashes as
    its integer, so ids made before heights were kept to 0.01 m still match.
    """
    lon = np.asarray(list(lons), dtype=float)
    group = np.zeros(len(lon), dtype=np.int64)
    return _multipoint_locations(group, lon, lats, alts, [altitude_ref])[0]


def _rounded(values: np.ndarray, decimals: int) -> np.ndarray:
    """
    Return each value as Python's ``round(value, decimals)`` gives it, for a whole array at once.

    ``rint(v * 10**d) / 10**d`` is the same double as ``round(v, d)``,
    except where ``v * 10**d`` lies within its rounding error of a half and
    the product can round to the other side. Those few values go through
    ``round`` itself, so the result is exact.
    """
    scale = 10.0**decimals
    scaled = values * scale
    out = np.rint(scaled) / scale
    for i in np.flatnonzero(np.abs(scaled - np.floor(scaled) - 0.5) < 1e-6):
        out[i] = round(float(values[i]), decimals)
    return out


def _multipoint_locations(
    groups: np.ndarray,
    lons: Iterable[float],
    lats: Iterable[float],
    alts: Iterable[float],
    refs: Sequence[str],
) -> dict[int, str]:
    """
    Return the location id of each multipoint release in a table of points, by group.

    *groups* numbers the release each point belongs to, and ``refs[g]`` is
    group ``g``'s vertical reference. The ids are :func:`_multipoint_location`'s:
    the JSON text of the sorted, rounded points, hashed. It is written out
    directly, with the floats as ``repr`` gives them as ``json`` does, and
    the rounding is done for all points at once.
    """
    lon = np.asarray(lons, dtype=float)
    lat = np.asarray(lats, dtype=float)
    alt = np.asarray(alts, dtype=float)
    order = np.lexsort((alt, lat, lon, groups))
    groups = np.asarray(groups)[order]
    rlon = _rounded(lon[order], 5).tolist()
    rlat = _rounded(lat[order], 5).tolist()
    ralt = _rounded(alt[order], 2)
    whole = (ralt == np.trunc(ralt)).tolist()
    points = [
        f"[{x!r},{y!r},{int(z) if w else repr(z)}]"
        for x, y, z, w in zip(rlon, rlat, ralt.tolist(), whole, strict=True)
    ]
    starts = np.flatnonzero(np.r_[True, groups[1:] != groups[:-1]]).tolist()
    ends = [*starts[1:], len(points)]
    locations = {}
    for start, end in zip(starts, ends, strict=True):
        canonical = "[" + ",".join(points[start:end]) + "]"
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:10]
        group = int(groups[start])
        locations[group] = f"multi_{digest}{_ref_suffix(refs[group])}"
    return locations


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


class Release(BaseModel):
    """
    Base class of the release types.

    A release is a frozen value: its release time, its release points, and
    the vertical reference of their heights. ``coords()`` gives
    ``(lon, lat, alt)`` for each release point. Two releases are equal when
    their type, time, points, and ``altitude_ref`` match; ``attrs`` do not
    count.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(description="Release type: point, column, or multipoint.")
    time: dt.datetime = Field(
        description=(
            "Release time, UTC. A time-zone-aware value is converted to UTC and "
            "a naive one taken as UTC. A string must be ISO 8601 or "
            "``YYYYMMDDHHMM``; other forms, such as the day-first "
            "``05/07/2023``, are refused."
        )
    )
    altitude_ref: VerticalReference = Field(
        default="agl",
        description="Whether heights are above ground (agl) or sea level (msl).",
    )
    attrs: dict[str, Any] = Field(
        default_factory=dict,
        exclude=True,
        repr=False,
        description=(
            "Extra labels, such as a site or scene name: the columns of "
            "``releases.csv`` that runtraj does not use. They are kept when the "
            "releases are written back, and are columns of "
            "``project.releases`` and ``project.simulations`` to select on. "
            "They are not part of the release's id or of equality."
        ),
    )

    @field_validator("time", mode="before")
    @classmethod
    def _parse_time(cls, value: Any) -> dt.datetime:
        """Parse the release time to a naive UTC datetime."""
        return parse_time(value)

    @field_validator("altitude_ref", mode="before")
    @classmethod
    def _lower_ref(cls, value: Any) -> Any:
        """Accept the altitude reference in any case."""
        return value.lower() if isinstance(value, str) else value

    # -- identity ----------------------------------------------------------

    @property
    def location_id(self) -> str:
        """Identifier of this release's location."""
        raise NotImplementedError

    @property
    def id(self) -> str:
        """Release id, the release time followed by the location id."""
        return f"{self.time:%Y%m%d%H%M}_{self.location_id}"

    def coords(self) -> list[tuple[float, float, float]]:
        """Return ``(lon, lat, alt)`` of each release point, the order ``from_points`` takes."""
        raise NotImplementedError

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return False
        other = cast(Release, other)
        return (
            self.time == other.time
            and self.altitude_ref == other.altitude_ref
            and self.coords() == other.coords()
        )

    def __hash__(self) -> int:
        return hash(
            (type(self).__name__, self.time, self.altitude_ref, tuple(self.coords()))
        )

    def __str__(self) -> str:
        return repr(self)

    # -- geometry ----------------------------------------------------------

    def _build_geometry(self) -> Geometry:
        """Return the shapely geometry of the release points."""
        raise NotImplementedError

    @cached_property
    def geometry(self) -> Geometry:
        """Shapely geometry of the release points."""
        return self._build_geometry()

    @property
    def points(self) -> list[Point]:
        """Release points as shapely ``Point(lon, lat, alt)`` objects."""
        shapely = _shapely()
        return [shapely.Point(lon, lat, alt) for lon, lat, alt in self.coords()]

    @cached_property
    def plot(self) -> ReleasePlotAccessor:
        """Plotting methods, such as ``release.plot.map()``."""
        from runtraj.visualization import ReleasePlotAccessor

        return ReleasePlotAccessor(self)

    # -- dicts -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return this release as a plain dict that :meth:`from_dict` reads back."""
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        """Return this release as JSON, as result files record it; :meth:`from_json` reads it back."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, text: str | bytes) -> Release:
        """Build a release from the JSON :meth:`to_json` wrote."""
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Release:
        """
        Build a release of the right type from a dict made by :meth:`to_dict`.

        Raises
        ------
        ValueError
            If the dict has no ``kind`` key, or names no known kind.
        """
        data = dict(d)
        if "kind" not in data:
            raise ValueError("Release dict must contain a 'kind' key.")
        return _ANY_RELEASE.validate_python(data)

    @classmethod
    def from_points(
        cls,
        time: TimeLike,
        points: Iterable[tuple[float, float, float]],
        *,
        altitude_ref: VerticalReference = "agl",
        attrs: Mapping[str, Any] | None = None,
    ) -> Release:
        """
        Build the right release type for a list of points.

        Parameters
        ----------
        time : datetime-like or str
            Release time (UTC).
        points : iterable of tuple of float
            ``(longitude, latitude, altitude)`` of each release point.
        altitude_ref : {"agl", "msl"}, default "agl"
            Whether altitudes are above ground level or above mean sea level.
        attrs : mapping, optional
            Extra labels.

        Returns
        -------
        Release
            A :class:`PointRelease` for one point, a :class:`ColumnRelease`
            for two points at the same longitude and latitude, and a
            :class:`MultiPointRelease` otherwise.
        """
        pts = [(float(lon), float(lat), float(alt)) for lon, lat, alt in points]
        if not pts:
            raise ValueError("At least one point must be provided.")
        labels = dict(attrs or {})
        time = parse_time(time)
        if len(pts) == 1:
            lon, lat, alt = pts[0]
            return PointRelease(
                time=time,
                longitude=lon,
                latitude=lat,
                altitude=alt,
                altitude_ref=altitude_ref,
                attrs=labels,
            )
        if len(pts) == 2 and pts[0][:2] == pts[1][:2]:
            bottom, top = sorted((pts[0][2], pts[1][2]))
            return ColumnRelease(
                time=time,
                longitude=pts[0][0],
                latitude=pts[0][1],
                bottom=bottom,
                top=top,
                altitude_ref=altitude_ref,
                attrs=labels,
            )
        lons, lats, alts = zip(*pts, strict=True)
        return MultiPointRelease(
            time=time,
            longitudes=lons,
            latitudes=lats,
            altitudes=alts,
            altitude_ref=altitude_ref,
            attrs=labels,
        )


class PointRelease(Release):
    """
    Release that releases particles from one point.

    Examples
    --------
    >>> r = PointRelease(
    ...     time="2023-07-15 18:00", longitude=-111.848, latitude=40.766, altitude=10
    ... )
    >>> r.id
    '202307151800_-111.848_40.766_10'
    """

    kind: Literal["point"] = Field(default="point", description="Release type.")
    longitude: float = Field(description="Longitude in degrees, from -180 to 180.")
    latitude: float = Field(description="Latitude in degrees, from -90 to 90.")
    altitude: float = Field(
        description="Release height in meters, above ground or sea level (``altitude_ref``)."
    )

    @model_validator(mode="after")
    def _check(self) -> PointRelease:
        """Check the coordinates and the height."""
        _raise_first(
            point_errors(
                [self.longitude], [self.latitude], [self.altitude], self.altitude_ref
            )
        )
        return self

    @property
    def location_id(self) -> str:
        """Location id, ``"<lon>_<lat>_<alt>"``, ending in ``msl`` above sea level."""
        return _point_location(
            self.longitude, self.latitude, self.altitude, self.altitude_ref
        )

    def coords(self) -> list[tuple[float, float, float]]:
        """Return ``(lon, lat, alt)`` of the release point."""
        return [(self.longitude, self.latitude, self.altitude)]

    def __repr__(self) -> str:
        return (
            f"PointRelease(id={self.id!r}, lon={self.longitude:.5f}, "
            f"lat={self.latitude:.5f}, alt={self.altitude:g} {self.altitude_ref})"
        )

    def _build_geometry(self) -> Point:
        """Return a shapely Point at the release point."""
        return _shapely().Point(self.longitude, self.latitude, self.altitude)


class ColumnRelease(Release):
    """
    Release that releases particles evenly along a vertical line.
    """

    kind: Literal["column"] = Field(default="column", description="Release type.")
    longitude: float = Field(description="Longitude in degrees, from -180 to 180.")
    latitude: float = Field(description="Latitude in degrees, from -90 to 90.")
    bottom: float = Field(
        description="Bottom of the column in meters. Must be less than ``top``."
    )
    top: float = Field(description="Top of the column in meters.")

    @model_validator(mode="after")
    def _check(self) -> ColumnRelease:
        """Check the coordinates and that the bottom is below the top."""
        _raise_first(
            [
                *point_errors(
                    [self.longitude] * 2,
                    [self.latitude] * 2,
                    [self.bottom, self.top],
                    self.altitude_ref,
                ),
                *column_errors([self.bottom], [self.top]),
            ]
        )
        return self

    @property
    def location_id(self) -> str:
        """Location id, ``"<lon>_<lat>_X<bottom>-<top>"``, ending in ``msl`` above sea level."""
        return _column_location(
            self.longitude, self.latitude, self.bottom, self.top, self.altitude_ref
        )

    def coords(self) -> list[tuple[float, float, float]]:
        """Return ``(lon, lat, alt)`` of the bottom and the top of the column."""
        return [
            (self.longitude, self.latitude, self.bottom),
            (self.longitude, self.latitude, self.top),
        ]

    def __repr__(self) -> str:
        return (
            f"ColumnRelease(id={self.id!r}, lon={self.longitude:.5f}, "
            f"lat={self.latitude:.5f}, bottom={self.bottom:g} {self.altitude_ref}, "
            f"top={self.top:g} {self.altitude_ref})"
        )

    def _build_geometry(self) -> LineString:
        """Return a shapely LineString from the bottom to the top."""
        return _shapely().LineString(
            [
                (self.longitude, self.latitude, self.bottom),
                (self.longitude, self.latitude, self.top),
            ]
        )


class MultiPointRelease(Release):
    """
    Release that releases particles from several points at once.

    Use it for a slanted column, such as a satellite sounding sampled at
    several heights along the line of sight. Points may share a horizontal
    location, but a transport model may refuse such a release: HYSPLIT joins
    them into a line source, so with HYSPLIT use a :class:`ColumnRelease` or
    one :class:`PointRelease` per height. A release of only two points at
    one location is refused here, since ``releases.csv`` reads it as a
    column.

    Raises
    ------
    ValueError
        If the arrays differ in length, a coordinate is out of range or not a
        finite number, or the release has exactly two points at one
        horizontal location.
    """

    kind: Literal["multipoint"] = Field(
        default="multipoint", description="Release type."
    )
    longitudes: tuple[float, ...] = Field(
        description="Longitude of each point in degrees, from -180 to 180."
    )
    latitudes: tuple[float, ...] = Field(
        description="Latitude of each point in degrees, from -90 to 90."
    )
    altitudes: tuple[float, ...] = Field(description="Height of each point in meters.")

    @field_validator("longitudes", "latitudes", "altitudes", mode="before")
    @classmethod
    def _as_floats(cls, value: Any) -> tuple[float, ...]:
        """Return the values, a list or 1-D array of numbers, as a tuple of floats."""
        if isinstance(value, (str, bytes)):
            raise ValueError(f"must be a list of numbers, not the text {value!r}")
        values = np.asarray(value, dtype=float)
        if values.ndim != 1:
            raise ValueError(
                f"must be a list or 1-D array of numbers; got shape {values.shape}"
            )
        return tuple(float(v) for v in values)

    @model_validator(mode="after")
    def _check(self) -> MultiPointRelease:
        """Check the points, and that two points are not at one location, which releases.csv reads as a column."""
        if not (len(self.longitudes) == len(self.latitudes) == len(self.altitudes)):
            raise ValueError(
                "longitudes, latitudes, and altitudes must have the same length."
            )
        if not self.longitudes:
            raise ValueError("A MultiPointRelease needs at least one point.")
        _raise_first(
            [
                *point_errors(
                    self.longitudes, self.latitudes, self.altitudes, self.altitude_ref
                ),
                *two_points_errors(self.longitudes, self.latitudes),
            ]
        )
        return self

    @property
    def location_id(self) -> str:
        """
        Location id, ``"multi_<hash>"``, ending in ``msl`` above sea level.

        The hash covers the sorted points, with longitudes and latitudes
        rounded to 5 decimals and altitudes to 0.01 m. A whole-meter
        altitude hashes as its integer.
        """
        return _multipoint_location(
            self.longitudes, self.latitudes, self.altitudes, self.altitude_ref
        )

    def coords(self) -> list[tuple[float, float, float]]:
        """Return ``(lon, lat, alt)`` of each release point."""
        return list(zip(self.longitudes, self.latitudes, self.altitudes, strict=True))

    def __repr__(self) -> str:
        return (
            f"MultiPointRelease(id={self.id!r}, n_points={len(self.coords())}, "
            f"altitude_ref={self.altitude_ref})"
        )

    def _build_geometry(self) -> MultiPoint:
        """Return a shapely MultiPoint of the release points."""
        return _shapely().MultiPoint(
            list(zip(self.longitudes, self.latitudes, self.altitudes, strict=True))
        )


#: Any release, for validating a dict of unknown kind.
AnyRelease = Annotated[
    PointRelease | ColumnRelease | MultiPointRelease, Field(discriminator="kind")
]
_ANY_RELEASE: TypeAdapter[Release] = TypeAdapter(AnyRelease)

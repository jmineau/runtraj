"""
The particle table: its columns, particle files, and release heights.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from runtraj._paths import location, readable, write_parquet
from runtraj.releases import (
    ColumnRelease,
    MultiPointRelease,
    Release,
    parse_release_id,
)

if TYPE_CHECKING:
    from upath import UPath


#: The columns every particle table has, as a particle file stores them:
#: what a transport model's run must return. ``particle`` is the number the
#: transport model gives the particle, ``age`` the minutes since release (negative for a
#: backward run), ``lon`` and ``lat`` the position in degrees, and ``zagl``
#: the height above ground in meters. The reference page on the particle
#: table says what the other columns are.
PARTICLE_SCHEMA = pa.schema(
    [
        ("particle", pa.int32()),
        ("age", pa.int32()),
        ("lon", pa.float64()),
        ("lat", pa.float64()),
        ("zagl", pa.float64()),
    ]
)

#: Particle columns stored as int32 rather than float64.
_INT_COLUMNS = ("age", "particle")

logger = logging.getLogger(__name__)


class ParticleMetadata(NamedTuple):
    """
    What a particle file records about the run that made it.

    Attributes
    ----------
    release : Release
        Release the particles were released from.
    settings : dict
        The run's settings, as its folder's ``_settings.yaml`` records them:
        the transport model's settings, the met's, the model build, and
        whether it is an ensemble. :func:`runtraj.identity.transport_from_settings`
        rebuilds the transport model's config from it.
    met_files : list of Path
        Meteorology files the run read.
    realization : int or None
        Which realization of an ensemble the particles are, which ran with
        ``seed + realization``; ``None`` for a single run.
    """

    release: Release
    settings: dict[str, Any]
    met_files: list[Path]
    realization: int | None = None


def check_particles(particles: pd.DataFrame, need: Iterable[str] = ()) -> None:
    """
    Check that a particle table has the columns of :data:`PARTICLE_SCHEMA`, and *need*.

    Parameters
    ----------
    particles : pandas.DataFrame
        A particle table, such as a transport model returns.
    need : iterable of str, optional
        Other columns the next step reads, such as ``("foot",)``.

    Raises
    ------
    ValueError
        Naming the missing columns.
    """
    wanted = dict.fromkeys([*PARTICLE_SCHEMA.names, *need])
    missing = [name for name in wanted if name not in particles.columns]
    if missing:
        raise ValueError(
            f"The particle table has no {', '.join(repr(m) for m in missing)} "
            f"column{'s' if len(missing) > 1 else ''}. Every particle table has "
            f"{', '.join(PARTICLE_SCHEMA.names)} (runtraj.particles.PARTICLE_SCHEMA)."
        )


def particles_metadata(path: str | Path | UPath) -> ParticleMetadata:
    """
    Return the release, run settings, and met files a particle file records.

    Raises
    ------
    ValueError
        If the file records no run settings, as files written before
        runtraj recorded them do.

    Examples
    --------
    >>> meta = runtraj.particles.particles_metadata(
    ...     sim.particles_path
    ... )  # doctest: +SKIP
    >>> meta.settings["model"]  # doctest: +SKIP
    {'name': 'hysplit', 'version': 'v5.1.0'}
    """
    with readable(path) as source:
        meta = pq.read_schema(source).metadata or {}
    if b"runtraj:settings" not in meta:
        raise ValueError(
            f"{path} records no run settings. It was written before particle "
            "files recorded them; rewrite its output directory."
        )
    realization = meta.get(b"runtraj:realization")
    return ParticleMetadata(
        release=stored_release(meta, path),
        settings=json.loads(meta[b"runtraj:settings"]),
        met_files=[Path(p) for p in json.loads(meta[b"runtraj:met_files"])],
        realization=None if realization is None else int(realization),
    )


def release_metadata(release: Release) -> dict[bytes, bytes]:
    """
    Return the Parquet schema metadata that records *release* in a result file.

    A stage that writes its own Parquet file merges this into the file's
    metadata, so that :func:`stored_release` reads the release back and the
    file is found as the release's.

    Parameters
    ----------
    release : Release
        The release the file was made for.

    Returns
    -------
    dict
        The metadata entries, to merge with the file's other entries.
    """
    return {b"runtraj:release": release.to_json().encode()}


def stored_release(meta: Mapping[bytes, bytes], path: str | Path | UPath) -> Release:
    """
    Return the release a stored result file records in its metadata.

    Parameters
    ----------
    meta : mapping
        The file's Parquet schema metadata.
    path : str or Path
        The file, named in the message when it cannot be read.

    Raises
    ------
    ValueError
        If the file was written before the receptor was renamed the release,
        so it records a ``receptor`` and not ``runtraj:release``, or
        records neither.
    """
    if b"runtraj:release" in meta:
        return Release.from_json(meta[b"runtraj:release"])
    if any(key.endswith(b":receptor") for key in meta):
        raise ValueError(OLD_FILE.format(what=f"The file {path}"))
    raise ValueError(f"{path} does not record its release.")


#: The message for a stored file that holds ``receptor`` where it now holds ``release``.
OLD_FILE = (
    "{what} was written before receptors were renamed releases, and is not "
    "read. Delete it and run again to make it with the current names: its "
    "settings folder is the same, so the simulation counts as unfinished "
    "once the file is gone."
)


def read_particles(
    path: str | Path | UPath, columns: list[str] | None = None
) -> pd.DataFrame:
    """
    Read a particle file.

    It needs nothing but the file: the release time in its metadata gives
    the ``time`` column (UTC) back, from ``age``. ``age`` and ``particle`` come back as
    float64, as HYSPLIT writes them. :func:`particles_metadata` reads the
    release and settings.

    Parameters
    ----------
    path : str or Path
        Particle file, or its URL on an object store.
    columns : list of str, optional
        Columns to read. All columns by default.

    Returns
    -------
    pandas.DataFrame
        One row per particle per output step.

    Examples
    --------
    >>> particles = runtraj.read_particles(sim.particles_path)  # doctest: +SKIP
    >>> particles.runtraj.endpoints()  # doctest: +SKIP
    """
    with readable(path) as source:
        pf = pq.ParquetFile(source)
        stored = pf.schema_arrow.names
        # The output directory adds a ``release`` column for scans of the
        # whole tree. One file is one release, so it is not read here.
        wanted = [c for c in (stored if columns is None else columns) if c != "release"]
        want_time = "time" in wanted or columns is None
        read = [c for c in wanted if c in stored]
        if want_time and "age" in stored and "age" not in read:
            read.append("age")  # time is built from it
        data = pf.read(columns=read).to_pandas()
    for name in _INT_COLUMNS:
        if name in data.columns:
            data[name] = data[name].astype("float64")
    if want_time and "age" in data.columns:
        meta = pf.schema_arrow.metadata or {}
        release = stored_release(meta, path)
        data["time"] = pd.Timestamp(release.time) + pd.to_timedelta(
            data["age"].to_numpy(), unit="min"
        )
    return data


def particles_from_table(table: pa.Table, time: bool = True) -> pd.DataFrame:
    """
    Return a table of many releases' particles as one DataFrame.

    The table is what :meth:`runtraj.output.Output.table` reads: a
    ``release`` column, the stored particle columns, and ``date``. The
    result has ``release`` first, ``age`` and ``particle`` as float64, and
    ``time`` (UTC) rebuilt from each release's time and ``age`` (unless
    *time* is false, or there is no ``age``); ``date`` is dropped.
    """
    data = table.unify_dictionaries().to_pandas()
    data = data.drop(columns=["date"], errors="ignore")
    if data.empty:
        return data
    # The release column is a dictionary of a few ids (a Categorical here), so
    # the strings and the times are made once per id and taken for every row:
    # converting each of millions of rows takes seconds.
    release = data["release"]
    if not isinstance(release.dtype, pd.CategoricalDtype):
        release = release.astype("category")
    ids = release.cat.categories.astype(str)
    codes = release.cat.codes.to_numpy()
    data["release"] = ids.take(codes)
    for name in _INT_COLUMNS:
        if name in data.columns:
            data[name] = data[name].astype("float64")
    if time and "age" in data.columns:
        release_time = pd.to_datetime([parse_release_id(r)[0] for r in ids])
        data["time"] = pd.Series(
            release_time.take(codes), index=data.index
        ) + pd.to_timedelta(data["age"].to_numpy(), unit="min")
    return data


def write_release_file(
    path: str | Path | UPath,
    data: pd.DataFrame,
    release: Release,
    settings: Mapping[str, Any],
    met_files: list[Path],
    metadata: Mapping[bytes, bytes] | None = None,
) -> Path | UPath:
    """
    Write *data*, one release's rows, to a Parquet file that holds the release, the settings and the met files.

    A ``release`` column, first, holds the release id, so a scan of many
    files can tell releases apart. The release, the run's *settings* and
    the *met_files* go in the file's metadata, with *metadata*. The path
    written to is returned.
    """
    table = pa.Table.from_pandas(data, preserve_index=False)
    table = table.add_column(
        0,
        pa.field("release", pa.dictionary(pa.int32(), pa.string())),
        pa.DictionaryArray.from_arrays(
            pa.array(np.zeros(table.num_rows, dtype=np.int32)),
            pa.array([str(release.id)]),
        ),
    )
    meta = {
        **release_metadata(release),
        b"runtraj:settings": json.dumps(dict(settings)).encode(),
        b"runtraj:met_files": json.dumps([str(p) for p in met_files]).encode(),
        **(metadata or {}),
    }
    return write_parquet(table.replace_schema_metadata(meta), location(path))


def write_particles(
    path: str | Path | UPath,
    particles: pd.DataFrame,
    release: Release,
    settings: Mapping[str, Any],
    met_files: list[Path],
    metadata: dict[bytes, bytes] | None = None,
) -> Path | UPath:
    """
    Write a particle table to a Parquet file that :func:`read_particles` reads alone.

    ``age`` and ``particle`` are stored as int32, and ``time`` is left out
    since it is the release time plus ``age``. A ``release`` column holds
    the release id, so a scan of many files can tell releases apart. The
    release, the run's settings, and the met files go in the file's
    metadata, so the file reads alone.

    Parameters
    ----------
    path : str or Path
        File to write.
    particles : pandas.DataFrame
        The particle table.
    release : Release
        Release the particles were released from.
    settings : mapping
        The run's settings, as its folder records them
        (:attr:`runtraj.config.Variant.run_settings`).
    met_files : list of Path
        Meteorology files the run used.
    metadata : dict, optional
        More file metadata, such as the settings hash.

    Returns
    -------
    Path
        The path written to.

    Raises
    ------
    ValueError
        If ``age`` or ``particle`` holds a value that is not a whole number.
    """
    data = particles.drop(columns=["time", "release"], errors="ignore")
    for name in _INT_COLUMNS:
        if name in data.columns:
            values = data[name].to_numpy()
            if not np.array_equal(values, np.round(values)):
                raise ValueError(f"Particle column {name!r} is not whole numbers.")
            data = data.assign(**{name: values.astype(np.int32)})
    return write_release_file(path, data, release, settings, met_files, metadata)


# -- after a model run: release heights ------------------------------------

# Below this horizontal spacing, release points cannot be told apart from a
# particle's first output row. In a test with HRRR at WBB, particles moved
# 200-600 m in the first minute, by an amount that varied with height. At
# 1000 m spacing the release height was recovered to about 15 m, and at 300 m
# it was off by about 190 m.
_MIN_RELIABLE_SPACING_M = 1000.0


def _closest_points_m(longitudes: np.ndarray, latitudes: np.ndarray) -> float:
    """Return the horizontal distance in meters between the two closest of at least two points."""
    x = longitudes * np.cos(np.radians(latitudes.mean())) * 111_320.0
    y = latitudes * 111_320.0
    gaps = np.hypot(x[:, None] - x[None, :], y[:, None] - y[None, :])
    return float(gaps[np.triu_indices(len(x), k=1)].min())


def needs_height_to_match(
    longitudes: Iterable[float],
    latitudes: Iterable[float],
    altitudes: Iterable[float],
) -> bool:
    """
    Return whether a multipoint release's particles can be told from their points only by height.

    HYSPLIT's release rows match them by height when every point has its
    own altitude and the run wrote no release (``t = 0``) rows
    (:func:`runtraj.models.hysplit.releases.release_rows`); for a
    release above sea level that height is ``zagl + zsfc``. Points that share
    an altitude are matched by position, and so are points more than
    ``1000 m`` apart, which position tells apart reliably. This is the one
    case left: every altitude differs and two points are closer than that.
    A model that writes release rows needs no height at all; this function
    does not know the model.
    """
    lons = np.asarray(list(longitudes), dtype=float)
    lats = np.asarray(list(latitudes), dtype=float)
    alts = np.asarray(list(altitudes), dtype=float)
    return (
        len(alts) > 1
        and len(np.unique(alts)) == len(alts)
        and _closest_points_m(lons, lats) < _MIN_RELIABLE_SPACING_M
    )


#: Columns of a table of release rows: where each particle started.
RELEASE_ROW_COLUMNS = ("particle", "lon", "lat", "height")


def column_release_rows(
    release: ColumnRelease, n_particles: int, n_slabs: int
) -> pd.DataFrame:
    """
    Return the release rows of a column's particles ``1`` to *n_particles*, released bottom to top.

    The column is split into *n_slabs* slabs of equal depth, and particle
    *k* starts at the center of slab ``(k - 1) % n_slabs`` (counted from
    the bottom): a transport model that releases the slabs once, as
    FLEXPART does, has *n_slabs* equal to *n_particles*, and one that
    releases them at every time step, as HYSPLIT does, fewer.
    """
    particle = np.arange(1, n_particles + 1)
    slab = (particle - 1) % n_slabs
    depth = (release.top - release.bottom) / n_slabs
    return pd.DataFrame(
        {
            "particle": particle,
            "lon": release.longitude,
            "lat": release.latitude,
            "height": release.bottom + (slab + 0.5) * depth,
        },
        columns=list(RELEASE_ROW_COLUMNS),
    )


def add_release_heights(
    particles: pd.DataFrame,
    release: Release,
    release_rows: Callable[[pd.DataFrame, Release], pd.DataFrame | None] | None = None,
) -> pd.DataFrame:
    """
    Return *particles* with each particle's ``release_height``, for a column or multipoint release.

    Each particle's release row says where it started: its row at
    ``age = 0``, or, for a transport model that writes none, the row
    *release_rows* gives (the model's ``release_rows``,
    :class:`runtraj.models.TransportModel`). For a multipoint release
    ``release_height`` is the altitude of the point the particle started
    at. For a column release it is the center of the particle's slab: the
    column is split into slabs of equal depth, one per release row, and the
    slab is what the particle stands for, not the random height inside it.
    A column's rows come from *release_rows* whenever it is given, even when
    the particles have rows at ``age = 0``: a particle does not start at the
    height its row at ``age = 0`` reports, so near a slab's edge that row can
    fall in the next slab, which then holds two particles while its own
    holds none. So the release rows must hold every particle released,
    including any missing from *particles*, such as one that left the met's
    domain before its first row.

    A point release's particles are returned as they are.

    Parameters
    ----------
    particles : pandas.DataFrame
        The particle table of one release.
    release : Release
        The release the particles started from.
    release_rows : callable, optional
        ``release_rows(particles, release)``, which returns a table with
        :data:`RELEASE_ROW_COLUMNS`, one row per particle released: where it
        started, its ``height`` in the release's vertical reference. It
        holds each particle of *particles* once, and may hold particles
        released but not in *particles*. Used for a column always, and for
        a multipoint release when the particles have no row at ``age = 0``,
        or their heights above sea level cannot be known without ``zsfc``.
        It returns ``None`` when it does not know, as
        :meth:`runtraj.models.TransportModel.release_rows` does by default.

    Raises
    ------
    ValueError
        If the particles have no release rows and *release_rows* is not
        given or returns ``None``, *release_rows* returns a table without its columns or
        without each particle once, or a release row's height is needed and
        cannot be known (``zsfc`` missing for a release above sea level).
    """
    if not isinstance(release, ColumnRelease | MultiPointRelease):
        return particles
    started = _started(particles, release, release_rows)
    if isinstance(release, ColumnRelease):
        heights = _column_heights(started, release)
    else:
        heights = _multipoint_heights(started, release)
    mapping = dict(zip(started["particle"].to_numpy(), heights, strict=True))
    return particles.assign(
        release_height=pd.Series(
            particles["particle"].to_numpy(), index=particles.index
        ).map(mapping.get)
    )


def _started(
    p: pd.DataFrame,
    release: Release,
    release_rows: Callable[[pd.DataFrame, Release], pd.DataFrame | None] | None,
) -> pd.DataFrame:
    """Return each particle's release row, with :data:`RELEASE_ROW_COLUMNS`."""
    # A column's slab is its particle's number, which the model's release_rows
    # knows; a row at age = 0 is only near the height the model chose.
    if isinstance(release, ColumnRelease) and release_rows is not None:
        given = release_rows(p, release)
        if given is not None:
            return _checked_rows(given, p, release_rows)
    released = p.loc[p["age"] == 0].drop_duplicates(subset="particle")
    rows = None
    if not released.empty and len(released) == p["particle"].nunique():
        rows = pd.DataFrame(
            {
                "particle": released["particle"].to_numpy(),
                "lon": released["lon"].to_numpy(dtype=float),
                "lat": released["lat"].to_numpy(dtype=float),
                "height": _height(released, release),
            }
        )
        if not rows["height"].isna().any():
            return rows
    # No row at age = 0, or heights a release above sea level cannot have
    # without zsfc: the model's own release rows may know them.
    if release_rows is not None:
        given = release_rows(p, release)
        if given is not None:
            return _checked_rows(given, p, release_rows)
    if rows is not None:
        return rows
    raise ValueError(
        f"The particles of {release.kind} release {release.id} have no row "
        "at age = 0, so where each one started is not known. A transport "
        "model that writes no such row gives release_rows."
    )


def _checked_rows(
    rows: pd.DataFrame, p: pd.DataFrame, release_rows: Callable[..., Any]
) -> pd.DataFrame:
    """Return the table *release_rows* gave, after checking it holds each particle of *p* once."""
    name = getattr(release_rows, "__qualname__", "release_rows")
    missing = [c for c in RELEASE_ROW_COLUMNS if c not in rows.columns]
    if missing:
        raise ValueError(f"{name} returned no {missing} column(s).")
    repeated = rows["particle"].duplicated()
    if repeated.any():
        raise ValueError(
            f"{name} returned more than one row for particle "
            f"{rows.loc[repeated, 'particle'].iloc[0]}."
        )
    absent = np.setdiff1d(p["particle"].unique(), rows["particle"].to_numpy())
    if len(absent):
        raise ValueError(f"{name} returned no row for particle {absent[0]}.")
    return rows[list(RELEASE_ROW_COLUMNS)].reset_index(drop=True)


def _height(rows: pd.DataFrame, release: Release) -> np.ndarray | float:
    """Return the rows' heights in the release's vertical reference, ``nan`` when they cannot be known."""
    if "zagl" not in rows.columns:
        return np.nan
    if release.altitude_ref == "agl":
        return rows["zagl"].to_numpy(dtype=float)
    if "zsfc" in rows.columns:
        return (rows["zagl"] + rows["zsfc"]).to_numpy(dtype=float)
    return np.nan


def _column_heights(started: pd.DataFrame, release: ColumnRelease) -> np.ndarray:
    """Return the center of the slab each particle started in, one slab per release row."""
    height = started["height"].to_numpy(dtype=float)
    if np.isnan(height).any():
        raise ValueError(
            f"Column release {release.id} is above sea level and its release "
            "rows have no zsfc, so the slab each particle started in is not "
            "known."
        )
    n_slabs = len(started)
    step = (release.top - release.bottom) / n_slabs
    slab = np.clip(np.floor((height - release.bottom) / step), 0, n_slabs - 1)
    return release.bottom + (slab + 0.5) * step


def _multipoint_heights(
    started: pd.DataFrame, release: MultiPointRelease
) -> np.ndarray:
    """
    Return the altitude of the point each particle started at.

    A particle started at the point nearest its release row horizontally;
    among points at one location, at the one nearest its height.
    """
    alts = np.asarray(release.altitudes, dtype=float)
    xy = started[["lon", "lat"]].to_numpy(dtype=float)
    points = np.column_stack(
        (
            np.asarray(release.longitudes, dtype=float),
            np.asarray(release.latitudes, dtype=float),
        )
    )
    d2 = np.sum((xy[:, None, :] - points[None, :, :]) ** 2, axis=2)
    nearest = d2 <= d2.min(axis=1, keepdims=True) + 1e-12
    if (nearest.sum(axis=1) > 1).any():
        height = started["height"].to_numpy(dtype=float)
        if np.isnan(height).any():
            raise ValueError(
                f"Multipoint release {release.id} has points at one location "
                "and is above sea level, and its release rows have no zsfc, so "
                "the point each particle started at is not known."
            )
        dz = np.where(nearest, np.abs(height[:, None] - alts[None, :]), np.inf)
        return alts[np.argmin(dz, axis=1)]
    return alts[np.argmin(d2, axis=1)]

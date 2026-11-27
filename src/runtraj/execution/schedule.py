"""
Running a project every hour, for fixed sites such as towers.

A project run on a schedule has a ``sites.csv``: the places that get a
release at each hour. ``runtraj schedule`` makes a Kubernetes CronJob
(:func:`runtraj.execution.kubernetes.schedule_job`) whose pod runs
``runtraj tick`` once an hour, which

1. adds a :class:`~runtraj.releases.PointRelease` per site at each hour of
   the last day that is at least ``delay`` hours old (``releases.csv``
   keeps the ones it has), reaching back further to close a gap left by an
   outage (:func:`due_hours`, :data:`MAX_BACKFILL_HOURS`),
2. writes ``releases.csv`` back where the inputs are, and
3. runs those releases, as ``runtraj run --releases`` would.

Steps 1 and 2 hold :func:`runtraj.execution.inputs.lock` on
``releases.csv`` (:func:`add_due_releases`), so a tick run by hand to fill
in a past day waits for the hourly one rather than writing over its hours.

Finished simulations are skipped, so a tick runs only what is new or what
failed before: an hour whose met was not there yet is tried again by every
tick for a day.

``delay`` is how long the met takes to appear. NOAA's HRRR archive posts
each 6-hour file about 40 minutes after its last hour, and a run also needs
the hour after its release, so an hour's met is all there at most about
7 hours 40 minutes later: :data:`DELAY_HOURS`.
"""

from __future__ import annotations

import csv
import logging
import math
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from runtraj._paths import is_url
from runtraj.execution.inputs import RELEASES, SITES, get, lock, put
from runtraj.project import Project
from runtraj.releases import PointRelease

logger = logging.getLogger(__name__)

#: Hours between an hour and the tick that first adds it: the met's lag.
DELAY_HOURS = 8
#: Hours back a tick adds, and runs again if they are not finished.
WINDOW_HOURS = 24
#: Extra hours, beyond the normal window, that one tick may reach back to
#: close a gap left by an outage: a week, which bounds one tick's work. A
#: longer outage is closed over the ticks that follow, each adding another
#: week (:func:`due_hours`).
MAX_BACKFILL_HOURS = 24 * 7
#: The most ``--delay`` may be: a week. A longer delay is a typo (``100000`` is
#: 11 years), and every hour it covers would be run against met that is not there yet.
MAX_DELAY_HOURS = 24 * 7
#: The columns ``sites.csv`` needs; ``altitude_ref`` (``agl`` or ``msl``) is optional.
SITE_COLUMNS = ("longitude", "latitude", "altitude")


def check_now(now: datetime | None) -> datetime:
    """
    Return *now* in UTC without a time zone, or the clock's time when it is ``None``.

    Raises
    ------
    ValueError
        If *now* is later than the clock: a tick can only add hours that
        are past, and a release in the future would hide a gap from every
        later tick.
    """
    clock = datetime.now(UTC).replace(tzinfo=None)
    if now is None:
        return clock
    if now.tzinfo is not None:
        now = now.astimezone(UTC).replace(tzinfo=None)
    if now > clock:
        raise ValueError(
            f"--now {now:%Y-%m-%dT%H:%M} is later than the clock "
            f"({clock:%Y-%m-%dT%H:%M} UTC): a tick can only add hours that are past."
        )
    return now


def due_hours(
    now: datetime, delay: int = DELAY_HOURS, since: datetime | None = None
) -> list[datetime]:
    """
    Return the hours a tick at *now* covers, oldest first, in UTC.

    The newest is *now* less *delay* hours, down to the hour; the oldest is
    normally :data:`WINDOW_HOURS` - 1 hours before it.

    *since*, when given, is the newest release time the project already has
    (in ``releases.csv``), rounded down to the hour. If the normal window would
    leave a gap after it, the window reaches back to close the gap instead, at most
    :data:`MAX_BACKFILL_HOURS` before its normal start: an outage longer
    than that is closed a week at a time, by the ticks that follow.

    Examples
    --------
    >>> hours = due_hours(datetime(2026, 10, 29, 18, 15), delay=8)
    >>> hours[0], hours[-1]
    (datetime.datetime(2026, 10, 28, 11, 0), datetime.datetime(2026, 10, 29, 10, 0))
    >>> due_hours(
    ...     datetime(2026, 10, 29, 18, 15), delay=8, since=datetime(2026, 10, 28, 0)
    ... )[0]
    datetime.datetime(2026, 10, 28, 1, 0)
    """
    newest = (now - timedelta(hours=delay)).replace(minute=0, second=0, microsecond=0)
    oldest = newest - timedelta(hours=WINDOW_HOURS - 1)
    if since is not None:
        since = since.replace(minute=0, second=0, microsecond=0)
    if since is not None and since < oldest - timedelta(hours=1):
        oldest = max(
            since + timedelta(hours=1), oldest - timedelta(hours=MAX_BACKFILL_HOURS)
        )
    span = int((newest - oldest) / timedelta(hours=1))
    return [oldest + timedelta(hours=k) for k in range(span + 1)]


def read_sites(directory: str | Path) -> list[dict[str, Any]]:
    """
    Return the sites in a project's ``sites.csv``.

    Each row needs ``longitude``, ``latitude`` and ``altitude``; an
    ``altitude_ref`` column (``agl``, the default, or ``msl``) is kept.
    Other columns, such as a site's name, are ignored.

    Raises
    ------
    FileNotFoundError
        If the project has no ``sites.csv``.
    ValueError
        If a column is missing (the message names the columns the file
        has), a value is empty or not a number, a site would not make a
        valid release (a latitude beyond 90, say), or there are no rows.
        The message names the line.

    Warns
    -----
    A site that repeats an earlier line is kept once, and logged: its
    releases would be the same ones.
    """
    path = Path(directory) / SITES
    if not path.is_file():
        raise FileNotFoundError(
            f"{Path(directory).name} has no {SITES}: a schedule needs the places "
            "to add a release at each hour (longitude, latitude, altitude)."
        )
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
    header = reader.fieldnames or []
    missing = [c for c in SITE_COLUMNS if c not in header]
    if not rows or missing:
        found = ", ".join(map(repr, header)) if header else "none"
        raise ValueError(
            f"{SITES} needs rows with columns {', '.join(SITE_COLUMNS)}; "
            f"its columns are {found} and it has {len(rows)} rows."
        )
    sites: list[dict[str, Any]] = []
    lines: dict[tuple, int] = {}
    for line, row in enumerate(rows, start=2):
        # A short row has None for what it lacks.
        empty = [c for c in SITE_COLUMNS if not (row[c] or "").strip()]
        if empty:
            raise ValueError(f"{SITES} line {line}: no {', '.join(empty)}.")
        try:
            site: dict[str, Any] = {c: float(row[c]) for c in SITE_COLUMNS}
        except ValueError:
            numbers = {c: row[c] for c in SITE_COLUMNS}
            raise ValueError(
                f"{SITES} line {line}: {numbers} is not all numbers."
            ) from None
        if (row.get("altitude_ref") or "").strip():
            site["altitude_ref"] = row["altitude_ref"].strip()
        if not all(math.isfinite(site[c]) for c in SITE_COLUMNS):
            raise ValueError(f"{SITES} line {line}: not all numbers.")
        try:
            PointRelease(time=datetime(2000, 1, 1), **site)
        except ValidationError as error:
            reasons = "; ".join(
                str(e["msg"]).removeprefix("Value error, ") for e in error.errors()
            )
            raise ValueError(f"{SITES} line {line}: {reasons}") from None
        key = tuple(site.get(c) for c in (*SITE_COLUMNS, "altitude_ref"))
        if key in lines:
            logger.warning(
                "%s line %d repeats line %d: the site is used once.",
                SITES,
                line,
                lines[key],
            )
            continue
        lines[key] = line
        sites.append(site)
    return sites


def hourly_releases(
    sites: Iterable[dict[str, Any]], hours: Iterable[datetime]
) -> list[PointRelease]:
    """
    Return a release at each site at each hour, hour by hour.

    Examples
    --------
    >>> site = {"longitude": -111.85, "latitude": 40.77, "altitude": 5.0}
    >>> [r.id for r in hourly_releases([site], [datetime(2026, 10, 29, 10)])]
    ['202610291000_-111.85_40.77_5']
    """
    sites = list(sites)
    return [PointRelease(time=hour, **site) for hour in hours for site in sites]


def _last_added(project: Project, now: datetime) -> datetime | None:
    """
    Return the newest hour in *project*'s ``releases.csv`` up to *now*, or ``None``.

    Rows after *now* are left out, so a release added for a later time
    cannot hide a gap from every later tick.
    """
    times = project.releases["time"]
    times = times[times <= now]
    if times.empty:
        return None
    return times.max().to_pydatetime()


def add_due_releases(
    location: str,
    directory: str | Path,
    *,
    now: datetime,
    delay: int = DELAY_HOURS,
) -> list[str]:
    """
    Add the releases now due to a project's ``releases.csv``, and return the ids of all that are due.

    The due hours are those of :func:`due_hours`, from the newest hour
    ``releases.csv`` holds; each site of ``sites.csv`` gets a release at
    each. Releases the file already holds are kept as they are, and their
    ids are returned too: a tick runs those that did not finish before.

    Parameters
    ----------
    location : str
        Where the project's inputs are: the URL they were copied to, or the
        project folder itself. ``releases.csv`` is locked there while it is
        read, added to, and written back.
    directory : str or Path
        The project folder on this machine: *location* itself, or the copy
        of its inputs.
    now : datetime
        The time to act as if it were, in UTC (:func:`check_now`).
    delay : int
        Hours before an hour is added (:data:`DELAY_HOURS`).

    Raises
    ------
    FileNotFoundError
        If the project has no ``sites.csv``.
    ValueError
        If ``sites.csv`` or ``releases.csv`` cannot be read.
    OSError
        If ``releases.csv`` cannot be locked, or copied from or back to
        *location*.
    """
    directory = Path(directory)
    sites = read_sites(directory)
    copy = is_url(location)
    # Another tick (a backfill by hand, say) may be adding its hours too. With
    # the lock, releases.csv is this tick's alone from a fresh copy to its put.
    with lock(location, RELEASES):
        if copy:
            get(location, directory, RELEASES)
        project = Project(directory)
        hours = due_hours(now, delay, since=_last_added(project, now))
        releases = hourly_releases(sites, hours)
        added = project.add_releases(releases)
        if added and copy:
            put(directory, location, RELEASES)
    logger.info(
        "Added %d releases, %d sites from %s to %s",
        len(added),
        len(sites),
        f"{hours[0]:%Y-%m-%d %H:%M}",
        f"{hours[-1]:%Y-%m-%d %H:%M}",
    )
    return [r.id for r in releases]


__all__ = [
    "DELAY_HOURS",
    "MAX_BACKFILL_HOURS",
    "MAX_DELAY_HOURS",
    "SITE_COLUMNS",
    "WINDOW_HOURS",
    "add_due_releases",
    "check_now",
    "due_hours",
    "hourly_releases",
    "read_sites",
]

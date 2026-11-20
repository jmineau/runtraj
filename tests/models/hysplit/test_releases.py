"""Tests for HYSPLIT's own release rule: no multipoint release whose points share a location."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from runtraj.exceptions import SimulationError
from runtraj.models import run_model
from runtraj.models.hysplit import HysplitConfig, HysplitModel
from runtraj.models.hysplit.releases import REPEATED_LOCATION, check_releases
from runtraj.particles.table import add_release_heights
from runtraj.releases import (
    ColumnRelease,
    MultiPointRelease,
    PointRelease,
    releases_to_frame,
)

TIME = dt.datetime(2023, 1, 1, 12)


def _multipoint(longitudes, latitudes) -> MultiPointRelease:
    return MultiPointRelease(
        time=TIME,
        longitudes=longitudes,
        latitudes=latitudes,
        altitudes=[100.0 * (i + 1) for i in range(len(longitudes))],
    )


@pytest.mark.parametrize(
    ("longitudes", "latitudes"),
    [
        ([-76.68869] * 5, [37.76396] * 5),  # stacked heights at one location
        ([-111.85, -111.86, -111.85], [40.77, 40.78, 40.77]),  # not next to each other
        ([-111.850001, -111.86, -111.85], [40.77, 40.78, 40.77]),  # equal to 5 places
    ],
)
def test_hysplit_refuses_a_multipoint_release_whose_points_share_a_location(
    longitudes, latitudes
):
    release = _multipoint(longitudes, latitudes)

    # Refused before hycs_std is looked for or the met is read.
    with pytest.raises(SimulationError, match="line source") as error:
        run_model("hysplit", release, HysplitConfig(n_hours=-1), None)
    assert error.value.reason == "RELEASE_REFUSED"


def test_hysplit_runs_points_at_their_own_locations():
    release = _multipoint([-111.85, -111.86, -111.87], [40.77, 40.78, 40.79])
    point = PointRelease(time=TIME, longitude=-111.85, latitude=40.77, altitude=5)
    rows = releases_to_frame([release, point])
    rows["release"] = [*[release.id] * 3, point.id]
    rows["kind"] = [*["multipoint"] * 3, "point"]

    assert HysplitModel().check_releases(rows, HysplitConfig(n_hours=-1)).empty


def test_check_releases_names_each_refused_release_once():
    bad = _multipoint([-111.85, -111.86, -111.85], [40.77, 40.78, 40.77])
    good = _multipoint([-111.85, -111.86, -111.87], [40.77, 40.78, 40.79])
    point = PointRelease(time=TIME, longitude=-111.85, latitude=40.77, altitude=5)
    rows = releases_to_frame([point, bad, good])
    rows["release"] = [point.id, *[bad.id] * 3, *[good.id] * 3]
    rows["kind"] = ["point", *["multipoint"] * 6]

    found = check_releases(rows)

    assert list(found.columns) == ["check", "release", "message"]
    assert found.to_dict("records") == [
        {"check": "release_refused", "release": bad.id, "message": REPEATED_LOCATION}
    ]
    assert check_releases(rows[rows["release"] != bad.id]).empty
    assert isinstance(check_releases(rows.iloc[:0]), pd.DataFrame)


def test_a_column_particle_missing_from_the_table_keeps_hysplits_slabs():
    """Particle k is in slab k of numpar, even when another particle has no row."""
    column = ColumnRelease(
        time=TIME, longitude=-111.85, latitude=40.77, bottom=0, top=1000
    )
    kept = [k for k in range(1, 11) if k != 4]  # particle 4 left before its first row
    particles = pd.DataFrame(
        {
            "particle": kept,
            "age": -1.0,
            "lon": -111.85,
            "lat": 40.77,
            "zagl": [100.0 * k - 50 for k in kept],
        }
    )

    found = add_release_heights(particles, column, HysplitModel().release_rows)

    assert found["release_height"].tolist() == [100.0 * k - 50 for k in kept]

"""Tests for HYSPLIT's own release rule: no multipoint release whose points share a location."""

from __future__ import annotations

import datetime as dt
from functools import partial

import pandas as pd
import pytest

from runtraj.exceptions import SimulationError
from runtraj.models import run_model
from runtraj.models.hysplit import HysplitConfig, HysplitModel
from runtraj.models.hysplit.releases import (
    REPEATED_LOCATION,
    check_releases,
    particles_per_step,
    release_rows,
)
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
    """Particle k is in slab k of the step, even when another particle has no row."""
    column = ColumnRelease(
        time=TIME, longitude=-111.85, latitude=40.77, bottom=0, top=800
    )
    kept = [k for k in range(1, 9) if k != 4]  # particle 4 left before its first row
    particles = pd.DataFrame(
        {
            "particle": kept,
            "age": -1.0,
            "lon": -111.85,
            "lat": 40.77,
            "zagl": [100.0 * k - 50 for k in kept],
        }
    )
    config = HysplitConfig(n_hours=-1, numpar=8)  # 8 particles in one step

    found = add_release_heights(
        particles, column, partial(HysplitModel().release_rows, config=config)
    )

    assert found["release_height"].tolist() == [100.0 * k - 50 for k in kept]


# Particles HYSPLIT 5.1.0 and 5.4.2 released in each one-minute step of a
# column release, counted from the first row of each particle in runs of
# hycs_std (outdt: 0) at -112.0, 40.5 on 2021-01-15 with HRRR.
@pytest.mark.parametrize(
    ("settings", "per_step"),
    [
        ({"numpar": 1000}, 1001),
        ({"numpar": 1000, "emisshrs": 0.02}, 834),
        ({"numpar": 1000, "emisshrs": 0.5}, 34),
        ({"numpar": 1200, "emisshrs": 0.5}, 41),
        ({"numpar": 500, "emisshrs": 0.3}, 28),
        ({"numpar": 777, "emisshrs": 0.1}, 130),
        ({"numpar": 2500, "emisshrs": 0.25}, 167),
        ({"numpar": 1000, "emisshrs": 0.7}, 24),
        ({"numpar": 1000, "emisshrs": 1.0}, 17),
        ({"numpar": 100, "emisshrs": 2.0}, 1),
        ({"numpar": 1000, "emisshrs": 0.5, "nbptyp": 2}, 17),
        ({"numpar": 1000, "emisshrs": 0.5, "maxdim": 2}, 34),
        ({"numpar": 1000, "emisshrs": 0.5, "initd": 2}, 1),
        ({"numpar": 1000, "emisshrs": 0.5, "initd": 103}, 34),
    ],
)
def test_particles_per_step_counts_as_hysplit_does(settings, per_step):
    assert particles_per_step(HysplitConfig(n_hours=-1, **settings)) == per_step


def test_particles_per_step_divides_numpar_among_the_locations():
    """HYSPLIT's ``NUMPAR/NRPTS``: 333 of 1000 over half an hour is 12 a minute."""
    config = HysplitConfig(n_hours=-1, numpar=1000, emisshrs=0.5)
    assert particles_per_step(config, 3) == 12
    assert particles_per_step(config, 1) == particles_per_step(config)


def test_a_column_released_over_several_steps_starts_each_step_at_the_bottom():
    """With emisshrs 0.5, each minute releases 34 particles up the whole column."""
    column = ColumnRelease(
        time=TIME, longitude=-111.85, latitude=40.77, bottom=5, top=1000
    )
    particles = pd.DataFrame({"particle": range(1, 1001), "age": -1.0})
    config = HysplitConfig(n_hours=-1, numpar=1000, emisshrs=0.5)

    rows = release_rows(particles, column, config).set_index("particle")

    slab = 995 / 34
    assert rows.loc[[1, 35, 69], "height"].tolist() == pytest.approx([5 + slab / 2] * 3)
    assert rows.loc[34, "height"] == pytest.approx(1000 - slab / 2)
    # The 30th step is cut short at MAXPAR: its 14 particles fill the bottom 14 slabs.
    assert rows.loc[1000, "height"] == pytest.approx(5 + 13.5 * slab)

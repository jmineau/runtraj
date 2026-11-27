"""
HYSPLIT's release rows, pinned for each HYSPLIT version.

The development branch (the coming 5.5) writes each particle a row at
``age = 0``, at the release, and runtraj takes release heights from it
(:func:`runtraj.particles.add_release_heights`). A test marked ``xfail`` on a
version pins a known HYSPLIT bug there: when HYSPLIT fixes it, the test
passes, which ``strict`` makes a failure, so runtraj's handling is looked at
again.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

import runtraj
from runtraj.exceptions import SimulationError
from runtraj.models.hysplit import HysplitConfig
from runtraj.releases import MultiPointRelease, PointRelease

from ..conftest import integration
from .conftest import REFERENCE_TIME, reference_met, run_raw

pytestmark = [integration, pytest.mark.usefixtures("hysplit_build")]

COLUMNS = ["time", "indx", "long", "lati", "zagl"]


def writes_release_rows(build: str) -> bool:
    """Return whether HYSPLIT *build* writes each particle a row at ``age = 0``."""
    return build == "dev"


def _rows_only(build):
    if not writes_release_rows(build):
        pytest.skip(f"HYSPLIT {build} writes no release rows")


def test_which_versions_write_release_rows(tmp_path, met_dir, hysplit_build):
    release = PointRelease(
        time=REFERENCE_TIME, longitude=-112.0, latitude=40.5, altitude=5.0
    )
    config = HysplitConfig(n_hours=-1, numpar=20, varsiwant=COLUMNS)
    particles = run_raw(tmp_path, release, config, met_dir)
    released = particles.loc[particles["age"] == 0, "particle"]
    if writes_release_rows(hysplit_build):
        assert sorted(released) == list(range(1, 21))
    else:
        assert released.empty


def test_a_release_over_half_an_hour_runs(tmp_path, met_dir, hysplit_build, request):
    # The development branch's release rows read met flags that its initdat.f
    # never sets, and some runs crash at the start; this one on every build tried.
    request.applymarker(
        pytest.mark.xfail(
            hysplit_build == "dev",
            reason="the development branch's release rows crash some runs",
            raises=SimulationError,
            strict=True,
        )
    )
    release = PointRelease(
        time=dt.datetime(2021, 1, 15, 6), longitude=-112.0, latitude=40.5, altitude=5.0
    )
    particles = runtraj.run_trajectories(
        release, reference_met(met_dir), model="hysplit", workdir=tmp_path,
        n_hours=1, numpar=100, emisshrs=0.5, krand=2, seed=7,
    )  # fmt: skip
    assert particles["particle"].nunique() == 100


def test_a_release_row_is_one_step_before_its_particles_first_row(
    tmp_path, met_dir, hysplit_build, request
):
    # Every release row says age 0, even for a particle released minutes
    # later. runtraj finds release rows at age 0, so a fix here needs a change
    # in runtraj too.
    _rows_only(hysplit_build)
    request.applymarker(
        pytest.mark.xfail(
            hysplit_build == "dev",
            reason="HYSPLIT writes every release row at age 0",
            strict=True,
        )
    )
    release = MultiPointRelease(
        time=REFERENCE_TIME,
        longitudes=[-112.0, -111.5],
        latitudes=[40.5, 41.0],
        altitudes=[5.0, 500.0],
    )
    config = HysplitConfig(n_hours=-1, numpar=100, emisshrs=0.5, varsiwant=COLUMNS)
    particles = run_raw(tmp_path, release, config, met_dir)

    # Backward, a particle's first two rows are its two largest ages.
    ages = particles.sort_values("age", ascending=False).groupby("particle")["age"]
    gaps = ages.agg(lambda a: a.iloc[0] - a.iloc[1])
    print(
        f"\nminutes from a release row to the next row: {gaps.value_counts().to_dict()}"
    )
    assert (gaps == 1).all()


def test_a_release_row_is_at_the_control_height(
    tmp_path, met_dir, hysplit_build, request
):
    # HYSPLIT turns a release height into a fraction of the model depth with
    # the terrain one minute downwind of the release, so a particle starts
    # meters off its height over sloped terrain. All versions do this; only the
    # release rows show it.
    _rows_only(hysplit_build)
    request.applymarker(
        pytest.mark.xfail(
            reason="HYSPLIT takes the terrain one minute downwind of a release",
            strict=True,
        )
    )
    release = MultiPointRelease(
        time=REFERENCE_TIME,
        longitudes=[-111.848, -111.65],
        latitudes=[40.766, 40.6],
        altitudes=[3000.0, 3000.0],
    )
    config = HysplitConfig(n_hours=-1, numpar=20, varsiwant=COLUMNS)
    particles = run_raw(tmp_path, release, config, met_dir)

    released = particles.loc[particles["age"] == 0, "zagl"]
    print(f"\nrelease rows minus 3000 m: {np.unique((released - 3000).round(3))}")
    np.testing.assert_allclose(released, 3000.0, atol=0.01)


def test_sphu_is_the_specific_humidity_of_rhfr_temp_and_pres(
    tmp_path, met_dir, hysplit_build, request
):
    # In the particle rows after the release, sphu is a fraction of the
    # specific humidity that the same rows' rhfr, temp and pres give.
    request.applymarker(
        pytest.mark.xfail(
            reason="HYSPLIT's particle rows write sphu several times too small",
            strict=True,
        )
    )
    release = PointRelease(
        time=REFERENCE_TIME, longitude=-112.0, latitude=40.5, altitude=5.0
    )
    config = HysplitConfig(
        n_hours=-1, numpar=20, varsiwant=[*COLUMNS, "temp", "rhfr", "pres", "sphu"]
    )
    rows = run_raw(tmp_path, release, config, met_dir)
    rows = rows.query("age != 0")

    celsius = rows["temp"] - 273.15
    vapor = rows["rhfr"] * 6.112 * np.exp(17.67 * celsius / (celsius + 243.5))
    expected = 0.622 * vapor / (rows["pres"] - 0.378 * vapor)
    ratio = float(np.median(rows["sphu"] / expected))
    print(f"\nsphu / specific humidity of rhfr, temp, pres: median {ratio:.3f}")
    assert ratio == pytest.approx(1.0, abs=0.1)

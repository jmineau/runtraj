"""
Integration tests that run a real FLEXPART 11 on ERA5.

They need a FLEXPART 11 checkout built with ``numpf = 3``, named by
``RUNTRAJ_TEST_FLEXPART_DIR`` (its ``src/FLEXPART``, and ``src/FLEXPART_ETA``
for the eta test), and a folder of ERA5 from flex_extract that covers
2023-06-01 04 to 18 UTC around Salt Lake City, named by
``RUNTRAJ_TEST_FLEXPART_MET_DIR``. Without them they skip
(CONTRIBUTING.md, "Testing against real FLEXPART").
"""

import datetime as dt
import os
import shutil
from pathlib import Path

import numpy as np
import pytest

import runtraj
from runtraj import Project
from runtraj.exceptions import SimulationError
from runtraj.models.flexpart.inputs import split
from runtraj.models.flexpart.met import catalog
from runtraj.releases import ColumnRelease, MultiPointRelease, PointRelease

from ...conftest import integration

eccodes = pytest.importorskip("eccodes")

pytestmark = integration

#: Hours back from each release: six outputs, an hour apart.
N_HOURS = -6
NUMPAR = 1000
OUTPUT_AGES = list(range(-60, -361, -60))  # minutes

#: Midday in Salt Lake City, when the mixed layer is deep.
NOON = "2023-06-01 18:00"
#: Before dawn, when it is shallow and particles keep their heights for an hour.
NIGHT = "2023-06-01 10:00"
SITE = {"longitude": -111.848, "latitude": 40.766}


@pytest.fixture(scope="session")
def flexpart_checkout() -> Path:
    """The FLEXPART 11 checkout ``RUNTRAJ_TEST_FLEXPART_DIR`` names, with ``src/FLEXPART`` built."""
    path = os.environ.get("RUNTRAJ_TEST_FLEXPART_DIR")
    if not path or not (Path(path) / "src" / "FLEXPART").is_file():
        pytest.skip(
            "Set RUNTRAJ_TEST_FLEXPART_DIR to a FLEXPART 11 checkout with "
            "src/FLEXPART built (numpf = 3)."
        )
    return Path(path)


@pytest.fixture(scope="session")
def era5() -> Path:
    """The folder of flex_extract's ERA5 that ``RUNTRAJ_TEST_FLEXPART_MET_DIR`` names."""
    path = os.environ.get("RUNTRAJ_TEST_FLEXPART_MET_DIR")
    if not path:
        pytest.skip(
            "Set RUNTRAJ_TEST_FLEXPART_MET_DIR to a folder of ERA5 from flex_extract."
        )
    return Path(path)


def run(release, checkout, met, **settings):
    """Run FLEXPART for *release* and return its particles."""
    return runtraj.run_trajectories(
        release,
        {"directory": str(met)},
        model="flexpart",
        directory=str(checkout),
        n_hours=N_HOURS,
        numpar=NUMPAR,
        **settings,
    )


def every_particle_every_hour(particles):
    """Check that no particle left the domain or went missing."""
    counts = particles.groupby("age").size()
    assert sorted(counts.index, reverse=True) == OUTPUT_AGES
    assert (counts == NUMPAR).all()


def test_point_release(flexpart_checkout, era5, tmp_path):
    release = PointRelease(time=NOON, altitude=25, **SITE)
    project = Project.init(
        tmp_path / "proj",
        [release],
        models={
            "flexpart": {
                "n_hours": N_HOURS,
                "numpar": NUMPAR,
                "directory": str(flexpart_checkout),
            }
        },
        mets={"era5": str(era5)},
        variants={"era5": {}},
        output=str(tmp_path / "out"),
    )

    project.run(compute_root=tmp_path / "compute")

    assert project.status()["state"].tolist() == ["complete"]
    particles = Project(tmp_path / "proj").particles()
    every_particle_every_hour(particles)
    # FLEXPART writes no met at the last output time (no gridded output).
    last = particles["age"] == OUTPUT_AGES[-1]
    assert particles.loc[last, "mlht"].isna().all()
    assert particles.loc[~last, ["zagl", "mlht", "dens", "pres"]].notna().all().all()
    # At midday the particles mix through the boundary layer within an hour
    # (on GFS, where FLEXPART derives no turbulence, they stayed near 8 m).
    first = particles[particles["age"] == -60]
    assert first["zagl"].median() > 500


def test_column_release(flexpart_checkout, era5):
    release = ColumnRelease(time=NIGHT, bottom=0, top=1000, **SITE)

    particles = run(release, flexpart_checkout, era5)

    every_particle_every_hour(particles)
    # Particle k starts in slab k of the column ...
    start = particles.groupby("particle")["release_height"].first()
    assert np.allclose(start, (start.index - 0.5) * 1000 / NUMPAR)
    # ... and an hour later, in the stable night, its height still says which.
    first = particles[particles["age"] == -60]
    assert np.corrcoef(first["release_height"], first["zagl"])[0, 1] > 0.9


def test_multipoint_release(flexpart_checkout, era5):
    points = {
        "longitudes": [-111.848, -112.5, -111.0],
        "latitudes": [40.766, 41.5, 40.0],
        "altitudes": [25.0, 500.0, 1000.0],
    }
    release = MultiPointRelease(time=NIGHT, **points)

    particles = run(release, flexpart_checkout, era5)

    every_particle_every_hour(particles)
    first = particles[particles["age"] == -60]
    # The points share the particles in turn, and an hour later each point's
    # particles are still near it.
    by_point = first.groupby("release_height")
    assert by_point.size().tolist() == split(NUMPAR, 3)
    near = by_point[["lon", "lat"]].median()
    assert np.allclose(near["lon"], points["longitudes"], atol=0.3)
    assert np.allclose(near["lat"], points["latitudes"], atol=0.3)


def test_one_thread_repeats_exactly(flexpart_checkout, era5):
    release = PointRelease(time=NOON, altitude=25, **SITE)

    first = run(release, flexpart_checkout, era5, threads=1)
    again = run(release, flexpart_checkout, era5, threads=1)

    assert first.equals(again)


def test_several_threads(flexpart_checkout, era5):
    release = PointRelease(time=NOON, altitude=25, **SITE)

    one = run(release, flexpart_checkout, era5, threads=1)
    four = run(release, flexpart_checkout, era5, threads=4)

    every_particle_every_hour(four)
    # Each thread draws its own random numbers, so the particles differ, but
    # their spread is the same.
    medians = [p.groupby("age")["zagl"].median() for p in (one, four)]
    assert np.allclose(*medians, rtol=0.2)


def test_eta_build(flexpart_checkout, era5):
    if not (flexpart_checkout / "src" / "FLEXPART_ETA").is_file():
        pytest.skip("The checkout has no src/FLEXPART_ETA (make eta=yes).")
    release = PointRelease(time=NOON, altitude=25, **SITE)

    particles = run(release, flexpart_checkout, era5, eta=True)

    every_particle_every_hour(particles)
    # The eta build writes no height at the last output time either.
    last = particles["age"] == OUTPUT_AGES[-1]
    assert particles.loc[last, "zagl"].isna().all()
    assert particles.loc[~last, "zagl"].median() > 500


def test_a_run_flexpart_stops_with_exit_status_0_fails(
    flexpart_checkout, era5, tmp_path
):
    """
    FLEXPART stops on a fourth precipitation field with a Fortran ``STOP``, which exits with status 0.

    The met is the test met with a large-scale precipitation message at step
    3, beyond the three that ``numpf = 3`` reads, added to the file FLEXPART
    reads its grid from. In a backward run that is the earliest one: the same
    message in the file at the release time does not stop it.
    """
    met = tmp_path / "met"
    met.mkdir()
    for path in era5.iterdir():
        (met / path.name).symlink_to(path)
    original = catalog(era5)[dt.datetime(2023, 6, 1, 12)]
    changed = met / original.name
    changed.unlink()
    shutil.copyfile(original, changed)
    with original.open("rb") as source:
        while (message := eccodes.codes_grib_new_from_file(source)) is not None:
            if eccodes.codes_get(message, "paramId") == 142:  # lsp
                break
            eccodes.codes_release(message)
    assert message is not None
    eccodes.codes_set(message, "stepRange", 3)
    with changed.open("ab") as out:
        eccodes.codes_write(message, out)
    eccodes.codes_release(message)
    release = PointRelease(time=NOON, altitude=25, **SITE)

    with pytest.raises(SimulationError, match="exit status 0") as caught:
        run(release, flexpart_checkout, met)

    assert caught.value.reason == "EXIT_STATUS"
    assert "STOP readwind_ecmwf: numpf too small" in caught.value.log

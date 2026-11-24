"""
Fixtures for the integration tests: releases and configs near Salt Lake City.

The tests run HYSPLIT on real meteorology, the test met cache that the
``met_dir`` fixture finds, and are marked ``integration``.
"""

import datetime as dt
from pathlib import Path

import pytest

from runtraj.config import ProjectConfig
from runtraj.models.hysplit import HysplitMetConfig
from runtraj.releases import ColumnRelease, MultiPointRelease, PointRelease
from tests.fixtures.factories import blocks, make_met_config

#: A time the test met cache covers, with six hours before it.
REFERENCE_TIME = dt.datetime(2021, 1, 15, 6, 0)

#: The HYSPLIT settings of the configs here: a short run of few particles,
#: with HYSPLIT's own defaults for the rest.
HYSPLIT = {"n_hours": -6, "numpar": 100}


def reference_met(met_dir: Path) -> HysplitMetConfig:
    """Return the met config of the test met cache: 6-hour HRRR files."""
    return make_met_config(met_dir)


@pytest.fixture(scope="session")
def wbb_release():
    """A point release 5 m above ground."""
    return PointRelease(
        time=REFERENCE_TIME, longitude=-112.0, latitude=40.5, altitude=5.0
    )


@pytest.fixture(scope="session")
def wbb_column_release():
    """Column release at the same place, 5 m to 1000 m."""
    return ColumnRelease(
        time=REFERENCE_TIME,
        longitude=-112.0,
        latitude=40.5,
        bottom=5.0,
        top=1000.0,
    )


@pytest.fixture(scope="session")
def wbb_multipoint_release():
    """Three-location multipoint release."""
    return MultiPointRelease(
        time=REFERENCE_TIME,
        longitudes=[-112.0, -111.5, -111.0],
        latitudes=[40.5, 41.0, 41.5],
        altitudes=[5.0, 500.0, 1000.0],
    )


@pytest.fixture(scope="session")
def traj_only_config(met_dir) -> ProjectConfig:
    """A project config for a short run of the test met cache."""
    return ProjectConfig(
        **blocks(
            mets={"hrrr": reference_met(met_dir)},
            **HYSPLIT,
            variants={"hrrr": {}},
        )
    )

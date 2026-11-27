"""
Fixtures for the integration tests: releases and configs near Salt Lake City.

The tests run HYSPLIT on real meteorology, the test met cache that the
``met_dir`` fixture finds, and are marked ``integration``.
"""

import datetime as dt
import os
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from runtraj.config import ProjectConfig
from runtraj.meteorology import run_window
from runtraj.models.hysplit import (
    HysplitConfig,
    HysplitMetConfig,
    Met,
    read_particle_dat,
    run_hycs_std,
    write_inputs,
)
from runtraj.models.hysplit.driver import (
    PARTICLE_FILE,
    SETUP_FILE,
    hysplit_version,
    version_at_least,
)
from runtraj.releases import ColumnRelease, MultiPointRelease, PointRelease, Release
from tests.fixtures.factories import blocks, make_met_config

#: A time the test met cache covers, with six hours before it.
REFERENCE_TIME = dt.datetime(2021, 1, 15, 6, 0)

#: The HYSPLIT settings of the configs here: a short run of few particles,
#: with HYSPLIT's own defaults for the rest.
HYSPLIT = {"n_hours": -6, "numpar": 100}


def reference_met(met_dir: Path) -> HysplitMetConfig:
    """Return the met config of the test met cache: 6-hour HRRR files."""
    return make_met_config(met_dir)


#: The HYSPLIT version ``just test-real`` runs, as ``hysplit-builds.toml``
#: names it: ``5.4.2``, or ``dev`` for the development branch, which prints 5.4.2.
BUILD_VARIABLE = "RUNTRAJ_HYSPLIT_VERSION"


@pytest.fixture
def hysplit_build(request) -> str:
    """
    Return the version of the real HYSPLIT on ``PATH``: :data:`BUILD_VARIABLE`, else what ``hycs_std`` prints.

    The tests that take it pin what each HYSPLIT version does, so a change in
    HYSPLIT fails them. They skip on replayed or recorded runs: a replay
    repeats one recording, so a run that differs each time would replay the same.
    """
    if request.config.getoption("--hysplit-replay") or request.config.getoption(
        "--hysplit-record"
    ):
        pytest.skip("pins a real HYSPLIT build's behavior, which a replay cannot show")
    return os.environ.get(BUILD_VARIABLE) or hysplit_version() or "unknown"


def since(build: str, minimum: tuple[int, int, int]) -> bool:
    """Return whether HYSPLIT *build* is *minimum* or later; ``dev``, the coming 5.5, is later than every release."""
    return build == "dev" or version_at_least(build, minimum)


def run_raw(
    workdir: Path,
    release: Release,
    config: HysplitConfig,
    met_dir: Path,
    edit: Callable[[str], str] | None = None,
) -> pd.DataFrame:
    """
    Run ``hycs_std`` on the inputs runtraj writes and return its particles as it wrote them.

    *edit* changes the text of ``SETUP.CFG`` first, to give HYSPLIT a value
    runtraj never writes.
    """
    met = Met("hrrr", reference_met(met_dir))
    files = met.files_for(run_window(release.time, config.n_hours))
    write_inputs(workdir, release.time, release.coords(), config, files)
    if edit is not None:
        setup = workdir / SETUP_FILE
        text = setup.read_text(encoding="utf-8")
        setup.write_text(edit(text), encoding="utf-8", newline="\n")
    run_hycs_std(workdir, timeout=300)
    return read_particle_dat(workdir / PARTICLE_FILE, config.varsiwant)


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

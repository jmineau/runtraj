"""Tests for runtraj.project: the project directory, its tables, its results, and running it."""

import datetime as dt

import matplotlib
import pandas as pd
import pytest

from runtraj import Project
from runtraj.config import ProjectConfig
from runtraj.output import Output
from runtraj.releases import PointRelease
from runtraj.transport.hysplit import MetConfig

from .fixtures.factories import make_met_config, make_release

matplotlib.use("Agg")

_XYERR = {
    "siguverr": 1.0,
    "tluverr": 60.0,
    "zcoruverr": 100.0,
    "horcoruverr": 10.0,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _met(tmp_path) -> MetConfig:
    return make_met_config(tmp_path / "met")


def _release(hour: int, longitude: float = -111.85, **attrs) -> PointRelease:
    return make_release(dt.datetime(2023, 1, 1, hour), longitude, **attrs)


def _particles() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "age": [-60.0, -120.0],
            "particle": [1.0, 1.0],
            "lon": [-113.9, -113.5],
            "lat": [39.7, 39.6],
            "zagl": [10.0, 20.0],
            "foot": [1e-5, 2e-5],
            "dens": [1.2, 1.2],
            "samt": [1.0, 1.0],
            "sigw": [0.1, 0.1],
            "tlgr": [10.0, 10.0],
            "mlht": [500.0, 500.0],
        }
    )


# ---------------------------------------------------------------------------
# Making and opening a project
# ---------------------------------------------------------------------------


def test_init_takes_the_releases_second(tmp_path, point_release):
    # The quickstart's order: a project directory, then its releases.
    project = Project.init(
        tmp_path / "proj",
        [point_release],
        mets={"hrrr": _met(tmp_path)},
        models={"hysplit": {"n_hours": -24}},
        variants={"hrrr": {}},
    )

    assert list(project.releases["release"]) == [point_release.id]


def test_init_writes_no_hysplit_values_for_another_model(tmp_path):
    config = ProjectConfig(
        mets={"hrrr": _met(tmp_path)},
        models={"mine": {"class": "tests.transport.test_models.ToyModel"}},
        variants={"hrrr": {"n_hours": -24}},
    )

    project = Project.init(tmp_path / "proj", config=config)

    assert "hysplit" not in project.config.models


def test_opening_reads_nothing_but_looks_for_config_yaml(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "config.yaml").write_text(
        "n_hours: [\n"
    )  # read only when asked
    project = Project(tmp_path / "proj")

    assert project.directory == (tmp_path / "proj").resolve()
    assert project.name == "proj"
    assert repr(project) == f"Project({str(project.directory)!r})"
    with pytest.raises(ValueError, match="not valid YAML"):
        _ = project.config


def test_a_path_that_is_not_a_project_is_refused_when_opened(tmp_path):
    """A mistyped path read as a project with no releases, with no warning."""
    with pytest.raises(FileNotFoundError, match=r"No config.yaml.*Project\.init"):
        Project(tmp_path / "my_projcet")
    assert not (tmp_path / "my_projcet").exists()
    (tmp_path / "loose").mkdir()
    (tmp_path / "loose" / "releases.csv").write_text(
        "time,longitude,latitude,altitude\n2023-01-01 12:00,-111.85,40.77,5\n"
    )
    with pytest.raises(FileNotFoundError, match="No config.yaml"):
        Project(tmp_path / "loose")


def test_a_relative_path_is_taken_from_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROJECTS", str(tmp_path / "env"))
    for folder in (tmp_path / "rel", tmp_path / "env" / "p"):
        folder.mkdir(parents=True)
        (folder / "config.yaml").write_text("")
    assert Project("rel").directory == (tmp_path / "rel").resolve()
    assert Project("$PROJECTS/p").directory == (tmp_path / "env" / "p").resolve()


# ---------------------------------------------------------------------------
# Output directory and scratch
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Variants and simulations
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Completion and status
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Loading results
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run() and submit()
# ---------------------------------------------------------------------------


@pytest.fixture
def ran(monkeypatch):
    """Record what a local run hands to the workers, and run nothing."""
    calls: list[dict] = []

    def run_releases(project, release_ids, **kwargs):
        calls.append({"project": project, "ids": list(release_ids), **kwargs})

    monkeypatch.setattr("runtraj.execution.worker.run_releases", run_releases)
    return calls


# ---------------------------------------------------------------------------
# A selection of simulations
# ---------------------------------------------------------------------------


def test_relative_paths_start_from_the_project_not_the_working_directory(
    tmp_path, monkeypatch, point_release
):
    monkeypatch.setenv("MET_ROOT", str(tmp_path / "archive"))
    (tmp_path / "real_output").mkdir()
    (tmp_path / "output_link").symlink_to(tmp_path / "real_output")
    Project.init(
        tmp_path / "proj",
        releases=[point_release],
        mets={
            "local": "met",
            "shared": {"directory": "$MET_ROOT/hrrr"},
        },
        models={"hysplit": {"n_hours": -24}},
        output=str(tmp_path / "output_link"),
        variants={"local": {}, "shared": {}},
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    project = Project(tmp_path / "proj")
    variants = project.variants
    assert variants["local"].met_config.directory == project.directory / "met"
    assert variants["shared"].met_config.directory == (
        (tmp_path / "archive/hrrr").resolve()
    )
    # Reached through a link, the output is the directory it points to.
    assert project.output == Output((tmp_path / "real_output").resolve())
    # A simulation finds relative file names in its settings from the project.
    assert project.simulation(point_release.id, "local").directory == project.directory


def test_init_refuses_a_variant_with_bad_settings_and_writes_nothing(tmp_path):
    """A variant's transport settings are checked before config.yaml is written."""
    met = make_met_config(str(tmp_path / "met"))
    with pytest.raises(ValueError, match="numpr"):
        Project.init(
            tmp_path / "proj", mets={"hrrr": met}, variants={"hrrr": {"numpr": 10}}
        )
    assert not (tmp_path / "proj" / "config.yaml").exists()


# ---------------------------------------------------------------------------
# Input tables
# ---------------------------------------------------------------------------

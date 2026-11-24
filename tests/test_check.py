"""Tests for Project.check: what would stop a project's runs, found before any run."""

import datetime as dt
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from runtraj import Project
from runtraj.releases import (
    PointRelease,
)
from runtraj.spatial import Bounds
from tests.fixtures.factories import blocks

from .fixtures.factories import (
    make_met_config,
    make_met_files,
    make_release,
    write_arl_file,
)

NOON = dt.datetime(2023, 1, 1, 12)
BOX = Bounds(xmin=-112.0, xmax=-111.0, ymin=40.0, ymax=41.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _release(
    hour: float = 12, longitude: float = -111.85, latitude: float = 40.77
) -> PointRelease:
    time = dt.datetime(2023, 1, 1) + dt.timedelta(hours=hour)
    return make_release(time, longitude, latitude)


def _project(
    tmp_path: Path,
    releases=None,
    *,
    met: dict[str, Any] | None = None,
    files: bool = True,
    n_hours: int = -3,
    **settings,
) -> Project:
    """
    Make a project whose hourly met files cover a run from noon, unless *met* or ``files=False``.

    *met* replaces the local met's settings, and *settings* the project's.
    """
    met_dir = tmp_path / "met"
    config = make_met_config(met_dir)
    if files:
        make_met_files(met_dir, NOON, n_hours)
    if met is not None:
        config = met
    settings.setdefault("variants", {"hrrr": {}})
    return Project.init(
        tmp_path / "proj",
        releases=[_release()] if releases is None else releases,
        **blocks(mets={"hrrr": config}, n_hours=n_hours, **settings),
    )


def _found(table: pd.DataFrame, check: str) -> pd.DataFrame:
    return table[table["check"] == check]


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def test_a_project_with_everything_in_place_has_nothing_to_check(tmp_path):
    table = _project(tmp_path).check()

    assert table.empty
    assert list(table.columns) == ["check", "variant", "release", "message"]


def test_a_project_with_no_releases_is_listed_once(tmp_path):
    """An empty project ran nothing, with no warning."""
    project = _project(tmp_path, variants={"hrrr": {}, "hrrr-zi": {"ziscale": 0.8}})
    project.releases_path.write_text("time,longitude,latitude,altitude\n")
    project = Project(project.directory)

    table = project.check()

    assert list(table["check"]) == ["releases"]
    assert list(table["variant"]) == ["hrrr, hrrr-zi"]
    assert table["release"].isna().all()
    assert "no releases" in table["message"].iloc[0]


def test_each_stage_of_a_variant_checks_its_own_inputs(tmp_path):
    """The check asks a stage it does not import, and variants with equal settings share its findings."""
    from .fixtures.stages import COUNT

    south = _release(latitude=-33.9)
    project = Project.init(
        tmp_path / "proj",
        releases=[_release(), south],
        mets={"hrrr": {"download": "hrrr", "directory": str(tmp_path / "met")}},
        models={"hysplit": {"n_hours": -3}},
        stages={"count": {"class": COUNT}},
        variants={
            "hrrr": {},
            "hrrr-zi": {"ziscale": 0.8},
            "tripled": {"stages": {"count": {"scale": 3}}},
            "plain": {"stages": {"count": None}},
        },
    )

    table = _found(project.check(), "count")

    assert list(table["release"]) == [south.id]
    assert list(table["variant"]) == ["hrrr, hrrr-zi, tripled"]
    assert list(table["message"]) == ["is south of the equator"]


# ---------------------------------------------------------------------------
# The met's directory
# ---------------------------------------------------------------------------


def test_a_met_directory_that_does_not_exist_is_found_once_for_the_met(tmp_path):
    releases = [_release(12), _release(13)]
    project = _project(
        tmp_path,
        releases,
        met={"directory": str(tmp_path / "typo")},
        files=False,
    )

    table = project.check()

    assert list(table["check"]) == ["met_directory"]
    assert table["release"].isna().all()
    assert f"{tmp_path / 'typo'} does not exist" in table["message"].iloc[0]


def test_a_met_directory_with_no_arl_file_is_found(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "README").write_text("not an ARL file")
    project = _project(tmp_path, met={"directory": str(empty)}, files=False)

    table = project.check()

    assert list(table["check"]) == ["met_directory"]
    assert "No ARL file" in table["message"].iloc[0]


def test_the_directory_is_only_a_download_destination_with_download(tmp_path):
    """A met with download makes its directory, and its files, when a run needs them."""
    project = _project(
        tmp_path,
        met={"directory": str(tmp_path / "later"), "download": "hrrr"},
        files=False,
    )

    assert project.check().empty


# ---------------------------------------------------------------------------
# The met's files
# ---------------------------------------------------------------------------


def test_a_release_missing_a_met_file_is_listed_with_its_hours(tmp_path):
    whole, short = _release(12), _release(20)
    project = _project(tmp_path, [whole, short])
    # The run of `short` covers 17:00 to 20:00; the files stop after 13:00.

    table = project.check()

    assert list(table["check"]) == ["met_files"]
    assert list(table["release"]) == [short.id]
    message = table["message"].iloc[0]
    assert message == "no met file for 2023-01-01 17:00 to 2023-01-01 20:00"


def _one_period_files(tmp_path: Path) -> list[Path]:
    """Rewrite the project's met files to hold one time period each, their own hour."""
    files = sorted((tmp_path / "met").glob("2023*"))
    for path in files:
        hour = pd.Timestamp(dt.datetime.strptime(path.name, "%Y%m%d_%H"))
        write_arl_file(path, times=[hour])
    return files


@pytest.mark.parametrize("n_hours", [-3, 4])
def test_met_files_that_hold_one_time_period_are_found_once_for_the_met(
    tmp_path, n_hours
):
    """HYSPLIT stops on a file with one time period, so every run of the met would fail."""
    project = _project(tmp_path, [_release(12), _release(13)], n_hours=n_hours)
    _one_period_files(tmp_path)

    table = project.check()

    assert list(table["check"]) == ["met_periods"]
    assert table["release"].isna().all()
    message = table["message"].iloc[0]
    assert "holds one time period" in message
    assert "Only one time period of meteo data" in message


def test_the_file_with_one_time_period_is_named(tmp_path):
    project = _project(tmp_path, [_release(12)])
    _one_period_files(tmp_path)

    table = project.check()

    # a backward run of 3 hours from 12:00 starts in the file of 09:00
    assert str(tmp_path / "met" / "20230101_09") in table["message"].iloc[0]


def test_a_missing_first_file_is_not_also_a_file_with_one_time_period(tmp_path):
    project = _project(tmp_path, [_release(12)])
    _one_period_files(tmp_path)
    (tmp_path / "met" / "20230101_09").unlink()

    assert "met_files" in set(project.check()["check"])


def test_a_file_cut_partway_through_a_time_step_is_named(tmp_path):
    """Its size is no whole number of time steps, which the header and the size show."""
    _block_met(tmp_path, 6)
    project = _project(tmp_path, [_release(9)], files=False)
    block = (tmp_path / "met" / "block_06").resolve()
    block.write_bytes(block.read_bytes()[:-10])  # cut inside a record

    table = project.check()

    # The file is not used, so the release lacks its hours too.
    assert list(table["check"]) == ["met_files", "met_cut"]
    cut = table.iloc[1]
    assert pd.isna(cut["release"])
    assert str(block) in cut["message"]


def test_two_products_holding_one_time_are_found_for_the_releases_they_stop(tmp_path):
    project = _project(tmp_path, [_release(12)])
    write_arl_file(
        tmp_path / "met" / "other_product",
        times=pd.date_range("2023-01-01 09:00", periods=2, freq="h"),
        source="NAM",
    )

    table = project.check()

    assert list(table["check"]) == ["met_files"]
    assert "its own folder" in table["message"].iloc[0]


def test_checking_reads_no_met_file_past_its_header(tmp_path, monkeypatch):
    """A whole read of a file is the run's: the check reads what a header says."""
    import arlmet

    project = _project(tmp_path, [_release(12), _release(13)])
    monkeypatch.setattr(arlmet, "File", MagicMock(side_effect=AssertionError))

    assert project.check().empty


def test_checking_finds_files_added_since_the_catalog_was_kept(tmp_path):
    project = _project(tmp_path, [_release(20)])
    assert list(project.check()["check"]) == ["met_files"]

    make_met_files(tmp_path / "met", _release(20).time, -3)

    assert project.check().empty


def _block_met(tmp_path, *starts):
    """Write a 6-hour file of hourly steps for each hour in *starts*, on 2023-01-01."""
    for start in starts:
        write_arl_file(
            tmp_path / "met" / f"block_{start:02d}",
            times=pd.date_range(f"2023-01-01 {start:02d}:00", periods=6, freq="h"),
        )


def test_a_met_file_the_hour_after_a_backward_run_needs_is_found_missing(tmp_path):
    """A backward run also reads the step after the release, which HYSPLIT interpolates."""
    _block_met(tmp_path, 6)
    project = _project(tmp_path, [_release(11.5)], files=False)

    table = project.check()

    assert list(table["message"]) == ["no met file for 2023-01-01 12:00"]


def test_a_met_file_the_hour_after_a_forward_run_needs_is_found_missing(tmp_path):
    """A forward run reads the step after its end too, so `runtraj check` lists it."""
    _block_met(tmp_path, 6)
    project = _project(tmp_path, [_release(8.5)], n_hours=3, files=False)

    table = project.check()

    assert list(table["message"]) == ["no met file for 2023-01-01 12:00"]


def test_a_variant_with_a_longer_run_is_listed_for_its_own_files(tmp_path):
    project = _project(
        tmp_path,
        [_release(12)],
        variants={"hrrr": {}, "hrrr-long": {"n_hours": -8}},
    )

    table = project.check()

    assert list(table["check"]) == ["met_files"]
    assert list(table["variant"]) == ["hrrr-long"]


def test_variants_that_share_a_met_and_a_run_length_share_their_rows(tmp_path):
    project = _project(
        tmp_path,
        [_release(20)],
        variants={"hrrr": {}, "hrrr-zi": {"ziscale": 0.8}},
    )

    table = project.check()

    assert list(table["check"]) == ["met_files"]
    assert list(table["variant"]) == ["hrrr, hrrr-zi"]


# ---------------------------------------------------------------------------
# The met's crop
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The met's domain
# ---------------------------------------------------------------------------


def test_a_release_on_the_edge_of_the_grid_is_inside_it(tmp_path):
    edge = _release(12, longitude=BOX.xmin, latitude=BOX.ymax)

    assert _project(tmp_path, [edge]).check().empty


# ---------------------------------------------------------------------------
# The averaging-kernel table
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# zsfc in varsiwant
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# More than one thing, and another transport model
# ---------------------------------------------------------------------------


def test_a_project_whose_files_are_invalid_cannot_be_checked(tmp_path):
    project = _project(tmp_path)
    project.config_path.write_text(project.config_path.read_text() + "nonsense: 1\n")

    with pytest.raises(ValueError, match="nonsense"):
        Project(project.directory).check()

"""Tests for Project.fetch and runtraj fetch: the met files the unfinished simulations need, got once."""

import datetime as dt
from pathlib import Path

import arlmet.archives
import pandas as pd
import pytest
from arlmet.archives import HRRRArchive
from typer.testing import CliRunner

from runtraj import models
from runtraj.models.hysplit import HysplitMetConfig, Met
from runtraj.project import Project
from tests.fixtures.factories import blocks

from .fixtures.factories import (
    make_met_config,
    make_met_files,
    make_release,
)

runner = CliRunner()

BOUNDS = {"xmin": -113.0, "xmax": -110.0, "ymin": 39.0, "ymax": 42.0}


class FakeHRRR(HRRRArchive):
    """
    HRRR's file names and 6 h blocks, with no network.

    ``_download`` and ``_fetch_and_crop`` write a few bytes and note the
    file, so a test can say what was downloaded, in how many requests, and
    that a second ask downloads nothing. A file whose URL holds a string in
    ``missing`` is not on the server. When ``error`` is set, every download
    raises it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.downloaded: list[str] = []
        self.requests: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        self.missing: set[str] = set()
        self.error: Exception | None = None

    def fetch(self, start, end, **kwargs):
        self.requests.append((pd.Timestamp(start), pd.Timestamp(end)))
        return super().fetch(start, end, **kwargs)

    def _download(self, url, dest, opts, **kwargs):
        if self.error is not None:
            raise self.error
        if any(text in url for text in self.missing):
            raise FileNotFoundError(f"{url} is not in the archive")
        self.downloaded.append(dest.name)
        dest.write_bytes(b"arl")

    def _fetch_and_crop(self, url, dest, opts, *, bbox, levels):
        self._download(url, dest, opts)


@pytest.fixture
def archive(monkeypatch) -> FakeHRRR:
    """The archive every met that downloads ``hrrr`` gets."""
    fake = FakeHRRR()
    monkeypatch.setattr(arlmet.archives, "get_archive", lambda name, **options: fake)
    return fake


def _release(day: int, hour: int):
    return make_release(dt.datetime(2024, 7, day, hour), -111.85, 40.77)


def _project(tmp_path: Path, releases, *, n_hours: int = -3, **met) -> Project:
    """A project that downloads HRRR into ``tmp_path / "met"``, cropped, and makes particles only."""
    fields = {
        "directory": tmp_path / "met",
        "download": "hrrr",
        "subgrid_enable": True,
        "subgrid_bounds": BOUNDS,
        "subgrid_levels": 4,
        **met,
    }
    return Project.init(
        tmp_path / "proj",
        releases=releases,
        **blocks(mets={"hrrr": fields}, n_hours=n_hours, variants={"hrrr": {}}),
    )


class _Stop(Exception):
    """Raised where HYSPLIT would start."""


def _files_a_run_gets(project: Project, release_ids, monkeypatch, tmp_path):
    """
    Return the met files that runs of *release_ids* ask for.

    Each is run through the transport model (:func:`runtraj.models.run_model`) up to
    the point where HYSPLIT would start, and the files :meth:`Met.files_for`
    returns are noted.
    """
    got: list[Path] = []
    files_for = Met.files_for

    def note(self, window):
        files = files_for(self, window)
        got.extend(files)
        return files

    def stop(*args, **kwargs):
        raise _Stop

    monkeypatch.setattr(Met, "files_for", note)
    monkeypatch.setattr(Met, "check", lambda self, files, window: None)
    monkeypatch.setattr("runtraj.models.hysplit.model.write_inputs", stop)
    for rid in release_ids:
        sim = project.simulation(rid, "hrrr")
        with pytest.raises(_Stop):
            models.run_model(
                sim.variant.model,
                sim.release,
                sim.transport,
                sim.variant.met_config,
                tmp_path / "workdir",
            )
    monkeypatch.undo()
    return got


# ---------------------------------------------------------------------------
# The files a run would ask for
# ---------------------------------------------------------------------------

#: Hours that reach across a block, fill one block, and are a week apart.
RELEASES = [
    _release(2, 3),
    _release(2, 7),
    _release(2, 8),
    _release(2, 20),
    _release(9, 1),
]


def test_fetch_gets_exactly_the_files_the_runs_ask_for(tmp_path, archive, monkeypatch):
    fetched = _project(tmp_path / "fetch", RELEASES, n_hours=-6)
    files = fetched.fetch()
    other = _project(tmp_path / "run", RELEASES, n_hours=-6)
    ran = _files_a_run_gets(
        other, other.releases["release"], monkeypatch, tmp_path / "wd"
    )

    assert files["message"].isna().all()
    assert len(files) == 7
    # Same names, in two met directories: the crop is in the name.
    assert sorted(Path(f).name for f in files["file"]) == sorted({p.name for p in ran})
    assert all(Path(f).is_file() for f in files["file"])
    assert set(files["met"]) == {"hrrr"}


def test_runs_after_a_fetch_download_nothing(tmp_path, archive, monkeypatch):
    project = _project(tmp_path, RELEASES, n_hours=-6)
    project.fetch()
    downloaded = list(archive.downloaded)

    ran = _files_a_run_gets(
        project, project.releases["release"], monkeypatch, tmp_path / "wd"
    )

    assert len(downloaded) == 7
    assert archive.downloaded == downloaded
    assert len(ran) > 0


def test_a_fetch_asks_the_archive_once_for_windows_that_overlap(tmp_path, archive):
    project = _project(tmp_path, RELEASES, n_hours=-6)

    project.fetch()

    # 2 July 03:00, 07:00 and 08:00 (-6 h) overlap; 20:00 and 9 July do not.
    assert len(archive.requests) == 3


# ---------------------------------------------------------------------------
# What is left to run
# ---------------------------------------------------------------------------


def test_a_selection_limits_the_fetch(tmp_path, archive):
    project = _project(tmp_path, RELEASES, n_hours=-6)
    sims = project.simulations

    files = project.fetch(sims[sims["release"] == RELEASES[-1].id])

    assert len(files) == 2  # 8 July 18-23 and 9 July 00-05
    assert all("202407" in f for f in files["file"])
    assert not any("20240702" in f for f in files["file"])


# ---------------------------------------------------------------------------
# Files that cannot be had
# ---------------------------------------------------------------------------


def test_a_file_the_archive_lacks_is_reported_for_its_times_alone(tmp_path, archive):
    # 2 July 06:00 and 12:00 (-12 h) overlap, so they are asked for together;
    # only the second needs the 12-17 block.
    releases = [_release(2, 6), _release(2, 12)]
    project = _project(tmp_path, releases, n_hours=-12)
    archive.missing = {"20240702_12-17"}

    files = project.fetch()

    failed = files[files["message"].notna()]
    assert len(failed) == 1
    assert "2024-07-02 00:00 to 2024-07-02 12:00" in failed["message"].iloc[0]
    assert "not in the archive" in failed["message"].iloc[0]
    assert failed["file"].isna().all()
    have = files[files["file"].notna()]
    assert len(have) == 3  # 1 July 18-23, 2 July 00-05 and 06-11
    assert all(Path(f).is_file() for f in have["file"])


def test_a_date_before_the_archive_begins_is_reported(tmp_path, archive):
    project = _project(
        tmp_path, [make_release(dt.datetime(2015, 1, 1, 12), -111.8, 40.7)]
    )

    files = project.fetch()

    assert files["file"].isna().all()
    assert "archive begins" in files["message"].iloc[0]


def test_a_met_without_download_is_only_checked(tmp_path):
    met_dir = tmp_path / "met"
    release = make_release(dt.datetime(2023, 1, 1, 12), -111.85, 40.77)
    make_met_files(met_dir, release.time, -1)  # a run of -3 h needs more
    project = Project.init(
        tmp_path / "proj",
        releases=[release],
        **blocks(
            mets={"hrrr": make_met_config(met_dir)}, n_hours=-3, variants={"hrrr": {}}
        ),
    )
    before = sorted(met_dir.iterdir())

    files = project.fetch()
    found = project.check()

    problems = files[files["message"].notna()]
    assert len(problems) == 1
    assert "2023-01-01 09:00" in problems["message"].iloc[0]
    assert list(found["check"]) == ["met_files"]
    assert "2023-01-01 09:00" in found["message"].iloc[0]
    assert sorted(met_dir.iterdir()) == before
    assert files["file"].isna().all()  # a run with a file missing has none to read


def test_a_met_without_download_that_is_complete_is_ready(tmp_path):
    met_dir = tmp_path / "met"
    release = make_release(dt.datetime(2023, 1, 1, 12), -111.85, 40.77)
    make_met_files(met_dir, release.time, -3)
    project = Project.init(
        tmp_path / "proj",
        releases=[release],
        **blocks(
            mets={"hrrr": make_met_config(met_dir)}, n_hours=-3, variants={"hrrr": {}}
        ),
    )

    files = project.fetch()

    assert files["message"].isna().all()
    assert len(files) == 4  # 09:00 to 12:00, the run's hours


# ---------------------------------------------------------------------------
# Met.fetch
# ---------------------------------------------------------------------------


def _window(start: str, end: str):
    return pd.Timestamp(start), pd.Timestamp(end)


def test_met_fetch_asks_once_for_overlapping_windows_and_apart_for_the_rest(
    tmp_path, archive
):
    met = Met("hrrr", HysplitMetConfig(directory=tmp_path, download="hrrr"))

    files = met.fetch(
        [
            _window("2024-07-02 00:00", "2024-07-02 06:00"),
            _window("2024-07-02 04:00", "2024-07-02 10:00"),
            _window("2024-07-02 10:00", "2024-07-02 11:00"),  # only touches
            _window("2024-07-02 00:00", "2024-07-02 06:00"),  # a repeat
        ]
    )

    assert len(archive.requests) == 2
    assert files["message"].isna().all()


def test_met_fetch_errors_other_than_a_missing_file_are_not_swallowed(
    tmp_path, archive, monkeypatch
):
    met = Met("hrrr", HysplitMetConfig(directory=tmp_path, download="hrrr"))

    def no_extra(*args, **kwargs):
        raise ImportError("fsspec is required")

    monkeypatch.setattr(archive, "fetch", no_extra)

    with pytest.raises(ImportError, match="fsspec"):
        met.fetch([_window("2024-07-02 00:00", "2024-07-02 06:00")])


# ---------------------------------------------------------------------------
# runtraj fetch
# ---------------------------------------------------------------------------

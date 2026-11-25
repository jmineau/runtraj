from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from runtraj.output import Output
from runtraj.particles import particles_metadata, write_particles

from .fixtures.output import (
    VARIANT,
    as_written_before_the_rename,
    fake_particles,
    output_variant,
    release_at,
    write_one,
)
from .fixtures.platform import not_on_windows

# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


def test_same_settings_under_another_name_share_the_folder(tmp_path):
    out = Output(tmp_path / "output")
    write_one(out)
    renamed = output_variant("hrrr-main")
    assert out.folder("particles", renamed) == out.folder("particles", VARIANT)
    write_one(out, renamed, release_at(hour=13))
    assert len(out.hashes("particles")) == 1


def test_changed_settings_make_a_new_folder_beside_the_old(tmp_path):
    out = Output(tmp_path / "output")
    write_one(out)
    changed = output_variant(ziscale=0.8)
    write_one(out, changed)
    first, second = out.folder("particles", VARIANT), out.folder("particles", changed)
    assert second != first
    assert second is not None and second.name.startswith("settings=hrrr-")
    assert set(out.hashes("particles")) == {
        first.name.removeprefix("settings="),
        second.name.removeprefix("settings="),
    }
    assert out.folder("particles", output_variant(numpar=7)) is None


# ---------------------------------------------------------------------------
# Particles
# ---------------------------------------------------------------------------


def test_an_empty_particle_table_is_not_written(tmp_path):
    """A particle file makes a simulation complete, so an empty one would hide a failure."""
    out = Output(tmp_path / "output")
    release = release_at()
    empty = fake_particles(release).iloc[0:0]

    with pytest.raises(ValueError, match="No particles"):
        out.write_particles(VARIANT, release, empty, [])
    assert out.present("particles", VARIANT) == set()


def test_particles_round_trip_in_date_folders(tmp_path):
    from runtraj.particles import read_particles

    out = Output(tmp_path / "output")
    release = release_at()
    traj = fake_particles(release)
    path = out.write_particles(VARIANT, release, traj, [])
    folder = out.folder("particles", VARIANT)
    assert path == folder / "date=2024-07-15" / f"{release.id}.parquet"
    assert path == out.path("particles", VARIANT, release.id)
    assert out.present("particles", VARIANT) == {release.id}

    back = read_particles(path)
    meta = particles_metadata(path)
    assert meta.release == release
    assert meta.settings == VARIANT.run_settings
    pd.testing.assert_frame_equal(
        back[traj.columns], traj, check_dtype=True, check_like=True
    )


def test_particles_store_time_and_index_as_int32(tmp_path):
    import pyarrow.parquet as pq

    path = write_one(Output(tmp_path / "output"))
    schema = pq.read_schema(path)
    assert str(schema.field("age").type) == "int32"
    assert str(schema.field("particle").type) == "int32"
    assert "time" not in schema.names
    assert str(schema.field("lon").type) == "double"


def test_particle_files_name_their_release_in_a_column(tmp_path):
    """A scan of the particles tree tells releases apart; one file reads without it."""
    from collections import Counter

    import pyarrow.dataset as pads

    from runtraj.particles import read_particles

    out = Output(tmp_path / "output")
    releases = [release_at(hour=6), release_at(hour=18, day=16)]
    for release in releases:
        out.write_particles(VARIANT, release, fake_particles(release, n=10), [])

    scan = pads.dataset(out.folder("particles", VARIANT), format="parquet").to_table()
    counts = Counter(scan.column("release").to_pylist())
    assert counts == {str(r.id): 100 for r in releases}

    back = read_particles(out.path("particles", VARIANT, releases[0].id))
    assert "release" not in back.columns


def test_particle_file_without_a_release_column_still_reads(tmp_path):
    """Files written before the release column read as before."""
    import pyarrow.parquet as pq

    from runtraj.particles import read_particles

    release = release_at()
    traj = fake_particles(release)
    path = write_particles(
        tmp_path / "p.parquet", traj, release, VARIANT.run_settings, []
    )
    table = pq.ParquetFile(path).read()
    old = table.drop_columns(["release"]).replace_schema_metadata(table.schema.metadata)
    pq.write_table(old, path)
    back = read_particles(path)
    pd.testing.assert_frame_equal(back[traj.columns], traj, check_like=True)


def test_particles_reject_fractional_time(tmp_path):
    release = release_at()
    traj = fake_particles(release)
    traj.loc[0, "age"] = -1.5
    with pytest.raises(ValueError, match="whole numbers"):
        Output(tmp_path / "output").write_particles(VARIANT, release, traj, [])


def test_particles_table_is_every_file_in_date_order(tmp_path):
    out = Output(tmp_path / "output")
    for day, hour in [(16, 0), (15, 18), (15, 6)]:
        write_one(out, release=release_at(hour=hour, day=day))
    table = out.table("particles", VARIANT)
    ids = list(dict.fromkeys(table.column("release").to_pylist()))
    assert [i[:10] for i in ids] == ["2024071506", "2024071518", "2024071600"]


# ---------------------------------------------------------------------------
# Files written before a receptor was renamed a release
# ---------------------------------------------------------------------------

OLD = "written before receptors were renamed releases"


def test_a_particle_file_from_before_the_rename_is_refused_with_its_name(tmp_path):
    from runtraj.particles import read_particles

    out = Output(tmp_path / "output")
    path = as_written_before_the_rename(write_one(out))
    for read in (particles_metadata, read_particles):
        with pytest.raises(ValueError, match=OLD) as error:
            read(path)
        assert str(path) in str(error.value)


def test_a_scan_of_particles_from_before_the_rename_is_refused(tmp_path):
    out = Output(tmp_path / "output")
    old = as_written_before_the_rename(write_one(out, release=release_at(hour=6)))
    with pytest.raises(ValueError, match=OLD) as error:
        out.table("particles", VARIANT)
    assert str(old) in str(error.value)
    with pytest.raises(ValueError, match=OLD):
        out.table("particles", VARIANT, columns=["lat"])


@pytest.mark.parametrize("old_first", [True, False])
def test_a_scan_of_old_and_new_files_names_an_old_one(tmp_path, old_first):
    """A dataset takes its columns from the first file, so either order must be caught."""
    out = Output(tmp_path / "output")
    first = write_one(out, release=release_at(hour=6))
    second = write_one(out, release=release_at(hour=18))
    old = as_written_before_the_rename(first if old_first else second)
    with pytest.raises(ValueError, match=OLD) as error:
        out.table("particles", VARIANT)
    assert str(old) in str(error.value)


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


def test_completed_is_the_one_rule():
    from runtraj.output import completed

    particles = frozenset({"a", "b", "c"})
    # A variant that makes particles only is complete with its particles.
    assert completed(particles) == {"a", "b", "c"}
    # With stages, a release needs the file of each one.
    assert completed(particles, frozenset({"b", "c", "d"})) == {"b", "c"}
    assert completed(particles, frozenset({"b", "c"}), frozenset({"c"})) == {"c"}
    assert completed(frozenset(), frozenset({"a"})) == frozenset()


# ---------------------------------------------------------------------------
# Failure records
# ---------------------------------------------------------------------------


def test_logs_and_kept_workdirs_sit_beside_the_particles(tmp_path):
    out = Output(tmp_path / "output")
    rid = release_at().id
    assert out.log_path(VARIANT, rid) is None and out.kept_workdir(VARIANT, rid) is None
    log = out.write_log(VARIANT, rid, "hycs_std ran\n")
    name = out.folder("particles", VARIANT).name
    assert log == tmp_path / "output" / "logs" / name / "date=2024-07-15" / f"{rid}.log"
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "CONTROL").write_text("x")
    kept = out.keep_workdir(VARIANT, rid, workdir)
    assert kept == out.kept_workdir(VARIANT, rid)
    assert (kept / "CONTROL").read_text() == "x"


# ---------------------------------------------------------------------------
# Two workers writing the same file at once
# ---------------------------------------------------------------------------


def _interleave(monkeypatch, competitor):
    """Make *competitor* run just before the first rename (or link) of the code under test."""
    import os

    state = {"raced": False}

    def racing(real):
        def move(src, dst):
            if not state["raced"]:
                state["raced"] = True
                competitor()
            real(src, dst)

        return move

    for name in ("replace", "rename", "link"):
        monkeypatch.setattr(os, name, racing(getattr(os, name)))


def test_two_workers_starting_a_run_at_once_both_succeed(tmp_path, monkeypatch):
    """Both wrote `_settings.tmp`; the second rename found it already moved."""
    path = tmp_path / "output"
    rid = release_at().id
    _interleave(monkeypatch, lambda: Output(path).write_log(VARIANT, rid, ""))

    Output(path).write_log(VARIANT, rid, "")

    folder = Output(path).folder("particles", VARIANT)
    record = yaml.safe_load((folder / "_settings.yaml").read_text())
    assert record["hash"] == VARIANT.result_hash
    assert [p.name for p in folder.iterdir()] == ["_settings.yaml"]


def test_two_projects_starting_the_same_settings_at_once_share_one_folder(
    tmp_path, monkeypatch
):
    """Each listed the tree before the other wrote, and once made a folder under its own variant's name."""
    path = tmp_path / "output"
    zeta, alpha = output_variant("zeta"), output_variant("alpha")
    first, second = Output(path), Output(path)
    _interleave(monkeypatch, lambda: write_one(second, alpha))

    write_one(first, zeta, release_at(hour=13))
    write_one(first, zeta, release_at(hour=14))

    assert first.folder("particles", zeta) == second.folder("particles", alpha)
    fresh = Output(path)
    assert list(fresh.hashes("particles")) == [f"alpha-{zeta.result_hash[:6]}"]
    assert len(fresh.present("particles", zeta)) == 3


def _settings_file(path):
    return Output(path).folder("particles", VARIANT) / "_settings.yaml"


def test_settings_another_worker_wrote_first_are_kept_while_it_reads_them(
    tmp_path, monkeypatch
):
    """Windows refuses a rename onto a file another process has open."""
    path = tmp_path / "output"
    rid = release_at().id
    Output(path).write_log(VARIANT, rid, "")
    settings = _settings_file(path)
    theirs = {**yaml.safe_load(settings.read_text()), "written_by": "the other worker"}
    settings.unlink()
    opened = []

    def other_worker():
        settings.write_text(yaml.safe_dump(theirs), encoding="utf-8")
        opened.append(settings.open(encoding="utf-8"))  # and a third reads it

    _interleave(monkeypatch, other_worker)
    try:
        Output(path).write_log(VARIANT, rid, "")
    finally:
        for file in opened:
            file.close()

    assert yaml.safe_load(settings.read_text())["written_by"] == "the other worker"
    assert [p.name for p in settings.parent.iterdir()] == ["_settings.yaml"]


def test_settings_another_worker_wrote_first_with_another_hash_are_refused(
    tmp_path, monkeypatch
):
    path = tmp_path / "output"
    rid = release_at().id
    Output(path).write_log(VARIANT, rid, "")
    settings = _settings_file(path)
    theirs = {**yaml.safe_load(settings.read_text()), "hash": "0" * 64}
    settings.unlink()
    _interleave(
        monkeypatch,
        lambda: settings.write_text(yaml.safe_dump(theirs), encoding="utf-8"),
    )

    with pytest.raises(FileExistsError, match="0000000000"):
        Output(path).write_log(VARIANT, rid, "")


def test_a_file_written_once_keeps_the_one_already_there(tmp_path):
    from runtraj._paths import atomic_path

    path = tmp_path / "settings.yaml"
    path.write_text("first", encoding="utf-8")

    with pytest.raises(FileExistsError), atomic_path(path, replace=False) as tmp:
        tmp.write_text("second", encoding="utf-8")

    assert path.read_text(encoding="utf-8") == "first"
    assert [p.name for p in tmp_path.iterdir()] == ["settings.yaml"]


@not_on_windows("Windows' rename keeps an existing file without a hard link")
def test_a_file_written_once_on_a_filesystem_without_hard_links_is_renamed(
    tmp_path, monkeypatch
):
    import errno
    import os

    from runtraj._paths import atomic_path

    def no_links(src, dst):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    path = tmp_path / "settings.yaml"

    with atomic_path(path, replace=False) as tmp:
        tmp.write_text("first", encoding="utf-8")

    assert path.read_text(encoding="utf-8") == "first"
    assert [p.name for p in tmp_path.iterdir()] == ["settings.yaml"]


def test_two_workers_writing_one_release_at_once_both_succeed(tmp_path, monkeypatch):
    """The other worker's cleanup removed the shared temporary file."""
    from runtraj.particles import read_particles

    release = release_at()
    traj = fake_particles(release)
    out = Output(tmp_path / "output")
    other = Output(tmp_path / "output")
    out.write_log(VARIANT, release.id, "")  # both find the folder
    other.folder("particles", VARIANT)
    _interleave(monkeypatch, lambda: other.write_particles(VARIANT, release, traj, []))

    path = out.write_particles(VARIANT, release, traj, [])

    assert len(read_particles(path)) == len(traj)
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no stray files


# ---------------------------------------------------------------------------
# Which releases have results
# ---------------------------------------------------------------------------


def test_present_lists_only_the_date_folders_asked_for(tmp_path, monkeypatch):
    """A few simulations of a large project must not list every date folder."""
    import runtraj.output as output_module

    out = Output(tmp_path / "output")
    day15, day16 = release_at(12, day=15), release_at(12, day=16)
    for release in (day15, day16):
        write_one(out, release=release)
    never_run = release_at(12, day=20)
    out.folder("particles", VARIANT)  # the folder is known; only releases are listed

    listed: list[str] = []
    real_scandir = output_module.os.scandir

    def scandir(path):
        listed.append(Path(path).name)
        return real_scandir(path)

    monkeypatch.setattr(output_module.os, "scandir", scandir)
    found = out.present("particles", VARIANT, [day15.id, never_run.id])

    assert found == {day15.id}
    assert sorted(listed) == ["date=2024-07-15", "date=2024-07-20"]
    assert out.present("particles", VARIANT) == {day15.id, day16.id}


@pytest.mark.parametrize(
    "release_id",
    [
        "202407151200_-111.85_40.77_5",
        "202407151259_-111.85_40.77_X0-100",
        "200002290000_-111.85_40.77_5msl",  # a leap day
        "202312312359_multi_0a1b2c3d",
        "000101010000_-111.85_40.77_5",
    ],
)
def test_the_date_folder_comes_from_the_start_of_the_id(release_id, monkeypatch):
    """The folder is the id's day; the time is not parsed for each id (a project lists hundreds of thousands)."""
    import runtraj.output as output_module
    from runtraj.releases import parse_release_id

    time, _ = parse_release_id(release_id)
    want = f"date={time:%Y-%m-%d}"
    assert output_module._date_dir(release_id) == want

    def parse(release_id):
        raise AssertionError("an id of the right form is not parsed")

    monkeypatch.setattr(output_module, "parse_release_id", parse)
    assert output_module._date_dir(release_id) == want
    assert output_module._date_dir(np.str_(release_id)) == want


@pytest.mark.parametrize(
    "release_id",
    [
        "",
        "202407151200",  # no location
        "202407151200_",
        "20240715120_-111.85_40.77_5",  # a digit short
        "2024071512000_-111.85_40.77_5",  # a digit over
        "202407151200-111.85_40.77_5",
        "2024071512ab_-111.85_40.77_5",
        "202413151200_-111.85_40.77_5",  # month 13
        "202402301200_-111.85_40.77_5",  # 30 February
        "202302290000_-111.85_40.77_5",  # 29 February of a common year
        "202407152400_-111.85_40.77_5",  # hour 24
        "202407151260_-111.85_40.77_5",  # minute 60
        "000001010000_-111.85_40.77_5",  # year 0
        "2024071512²0_-111.85_40.77_5",  # a digit that is not 0 to 9
        "2024-07-15_12_00_-111.85_40.77_5",
    ],
)
def test_a_malformed_id_has_no_date_folder_and_says_why(release_id):
    import runtraj.output as output_module

    with pytest.raises(ValueError, match="is not of the form"):
        output_module._date_dir(release_id)


# ---------------------------------------------------------------------------
# What each file records
# ---------------------------------------------------------------------------


def test_a_lookup_reads_each_folder_once(tmp_path, monkeypatch):
    """A lookup that misses lists the tree again but reads only the folders it has not seen."""
    out = Output(tmp_path / "output")
    write_one(out)
    write_one(out, output_variant(ziscale=0.8))

    import runtraj.output

    reads: list[str] = []
    real = runtraj.output.read_run_settings

    def counting(stored):
        reads.append(stored["numpar"])
        return real(stored)

    monkeypatch.setattr(runtraj.output, "read_run_settings", counting)
    fresh = Output(tmp_path / "output")
    for _ in range(3):  # a variant that has not run misses every time
        assert fresh.folder("particles", output_variant(numpar=7)) is None
    assert len(reads) == 2  # each existing folder once

    # A folder another worker creates is found on the next lookup, which
    # reads that folder alone.
    write_one(Output(tmp_path / "output"), output_variant(numpar=7))
    before = len(reads)
    assert fresh.folder("particles", output_variant(numpar=7)) is not None
    assert len(reads) == before + 1


def test_an_ensembles_realizations_are_partitions_of_one_folder(tmp_path):
    from dataclasses import replace

    out = Output(tmp_path / "output")
    ensemble = replace(output_variant("err", krand=4), realizations=2)
    release = release_at()
    paths = [
        out.write_particles(ensemble, release, fake_particles(release), [], k)
        for k in (0, 1)
    ]
    folder = out.folder("particles", ensemble)
    assert [p.parent.parent for p in paths] == [
        folder / "realization=0",
        folder / "realization=1",
    ]
    assert particles_metadata(paths[1]).realization == 1
    assert out.present("particles", ensemble, realization=1) == {release.id}
    assert out.present("particles", ensemble, [release.id], 0) == {release.id}
    out.record_failure("particles", ensemble, release.id, {"reason": "X"}, 1)
    assert out.failure("particles", ensemble, release.id, 1) == {"reason": "X"}
    assert out.failure("particles", ensemble, release.id, 0) is None
    with pytest.raises(ValueError, match="realizations"):
        out.path("particles", ensemble, release.id)  # an ensemble needs its number
    with pytest.raises(ValueError, match="realizations"):
        out.path("particles", VARIANT, release.id, 0)
    table = out.table("particles", ensemble, realization=0)
    assert set(table.column("release").to_pylist()) == {release.id}


def test_a_kept_workdir_can_leave_out_named_files(tmp_path):
    out = Output(tmp_path / "output")
    rid = release_at().id
    workdir = tmp_path / "work"
    (workdir / "sub").mkdir(parents=True)
    (workdir / "CONTROL").write_text("x")
    (workdir / "PARTICLES").write_text("big")
    (workdir / "sub" / "PARTICLES").write_text("not at the top")
    kept = out.keep_workdir(VARIANT, rid, workdir, leave_out={"PARTICLES"})
    assert sorted(p.name for p in kept.iterdir()) == ["CONTROL", "sub"]
    assert (kept / "sub" / "PARTICLES").exists()

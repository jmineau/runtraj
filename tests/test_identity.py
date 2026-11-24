import hashlib
from dataclasses import replace

import pytest
import yaml

import runtraj
from runtraj.config import ProjectConfig, Variant
from runtraj.exceptions import HYSPLITNotFoundError
from runtraj.identity import read_run_settings, run_settings, settings_hash, stage_hash
from runtraj.models import ModelInfo
from runtraj.models.hysplit import HysplitConfig, HysplitMetConfig
from runtraj.models.hysplit.model import hysplit_version
from runtraj.output import Output
from tests.fixtures.factories import blocks

from .conftest import STAND_IN_VERSION
from .fixtures.factories import (
    make_hysplit_dir,
    make_met_config,
    program_path,
    write_arl_file,
)

GRID = {"xmin": -112, "xmax": -111, "ymin": 40, "ymax": 41, "xres": 0.1, "yres": 0.1}


def _met(tmp_path, **overrides) -> HysplitMetConfig:
    return make_met_config(tmp_path / "met", **overrides)


def _variant(tmp_path, met: HysplitMetConfig | None = None, **transport) -> Variant:
    """The resolved ``hrrr`` variant of a one-met project with these transport fields."""
    config = ProjectConfig(
        **blocks(
            mets={"hrrr": met or _met(tmp_path)},
            **{"variants": {"hrrr": {}}, **transport},
        )
    )
    return config.resolve()["hrrr"]


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# the model build
# ---------------------------------------------------------------------------


def test_hysplit_version_is_what_hycs_std_prints(tmp_path):
    folder = make_hysplit_dir(tmp_path / "b", "hysplit.v5.2.2")
    assert hysplit_version(folder) == "hysplit.v5.2.2"


def test_hysplit_version_without_directory_is_of_the_hycs_std_on_path():
    assert hysplit_version() == STAND_IN_VERSION


def test_hysplit_version_is_none_when_hycs_std_prints_none(tmp_path):
    assert hysplit_version(make_hysplit_dir(tmp_path / "b", version=None)) is None


def test_hysplit_version_is_none_for_a_hycs_std_that_cannot_run_here(tmp_path):
    """Another platform's build: the version is recorded only, so it does not stop a run."""
    folder = make_hysplit_dir(tmp_path / "b")
    program_path(folder / "exec").write_bytes(b"\x00not a program for this machine")
    assert hysplit_version(folder) is None


def test_hysplit_version_is_none_for_a_hycs_std_that_does_not_stop(
    tmp_path, monkeypatch
):
    from runtraj.models.hysplit import model

    monkeypatch.setattr(model, "_VERSION_TIMEOUT", 0.5)
    folder = make_hysplit_dir(tmp_path / "b", version=None, body="exec sleep 30\n")
    assert hysplit_version(folder) is None


def test_without_hycs_std_hysplit_version_says_what_to_set(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(HYSPLITNotFoundError, match="directory.*PATH"):
        hysplit_version()


def test_the_build_is_the_checksum_of_hycs_std(tmp_path):
    """Two builds that print one version are two runs; the version is recorded beside."""
    one = _variant(tmp_path, directory=make_hysplit_dir(tmp_path / "one", body="# 1\n"))
    two = _variant(tmp_path, directory=make_hysplit_dir(tmp_path / "two", body="# 2\n"))
    exe = program_path(tmp_path / "one" / "exec")
    assert one.build.sha256 == hashlib.sha256(exe.read_bytes()).hexdigest()
    assert one.build.version == two.build.version == "hysplit.v5.1.0"
    assert one.run_settings["model"] == {
        "name": "hysplit",
        "sha256": one.build.sha256,
        "data_files": one.build.data_files,
    }
    assert one.build.data_files == two.build.data_files
    assert one.result_hash != two.result_hash


def test_moving_the_hysplit_folder_keeps_the_hash(tmp_path):
    one = _variant(tmp_path, directory=make_hysplit_dir(tmp_path / "one"))
    moved = _variant(tmp_path, directory=make_hysplit_dir(tmp_path / "moved"))
    assert "directory" not in one.run_settings
    assert one.result_hash == moved.result_hash


# ---------------------------------------------------------------------------
# what a run's settings hold
# ---------------------------------------------------------------------------


def test_a_runs_met_is_its_product_and_crop(tmp_path):
    met = _met(tmp_path, subgrid_dir=tmp_path / "sub")
    recorded = run_settings(
        HysplitConfig(n_hours=-24), met.settings(), ModelInfo(name="hysplit")
    )
    assert recorded["met"] == {"source": "HRRR", "crop": None}


def test_moving_or_renaming_the_met_files_keeps_the_hash(tmp_path):
    base = _variant(tmp_path, numpar=100)
    write_arl_file(tmp_path / "moved" / "hrrr.20240718.t00z")
    moved = _met(tmp_path).model_copy(update={"directory": tmp_path / "moved"})
    assert _variant(tmp_path, met=moved, numpar=100).result_hash == (base.result_hash)


def test_two_products_named_alike_are_two_runs(tmp_path):
    """An HRRR and a NAM archive with the same file names hash apart (R2-24)."""
    hrrr = _variant(tmp_path, numpar=100)
    write_arl_file(tmp_path / "nam" / "20240718_00", source="NAM")
    nam = _met(tmp_path).model_copy(update={"directory": tmp_path / "nam"})
    assert _variant(tmp_path, met=nam, numpar=100).result_hash != (hrrr.result_hash)


def test_a_single_run_records_no_realization(tmp_path):
    recorded = _variant(tmp_path).run_settings
    assert "realization" not in recorded
    assert "ensemble" not in recorded


def test_run_settings_leave_out_what_changes_no_particle(tmp_path):
    base = _variant(tmp_path, numpar=100)
    recorded = base.run_settings
    assert "directory" not in recorded
    assert set(recorded["model"]) == {"name", "sha256", "data_files"}
    # Unset, maxpar depends on the release, so it is recorded unset,
    # and a set maxpar, which can stop a multipoint release, is another run.
    assert recorded["maxpar"] is None
    capped = _variant(tmp_path, numpar=100, maxpar=100)
    assert capped.run_settings["maxpar"] == 100
    assert capped.result_hash != base.result_hash
    assert _variant(tmp_path, numpar=200).result_hash != base.result_hash


def test_where_met_is_downloaded_from_does_not_identify_the_run(tmp_path):
    base = _variant(tmp_path, numpar=100)
    for override in ({"download_from": "ftp"},):
        other = _variant(tmp_path, met=_met(tmp_path, **override), numpar=100)
        assert other.result_hash == base.result_hash


def test_nested_and_flat_ziscale_are_one_run(tmp_path):
    """``[[0.8, 0.9]]`` and ``[0.8, 0.9]`` are the same factors and the same hash."""
    flat = _variant(tmp_path, ziscale=[0.8, 0.9])
    nested = _variant(tmp_path, ziscale=[[0.8, 0.9]])
    assert nested.transport.ziscale == [0.8, 0.9]
    assert nested.result_hash == flat.result_hash
    with pytest.raises(ValueError, match="Per-simulation"):
        HysplitConfig(n_hours=-24, ziscale=[[0.8], [0.9]])


# ---------------------------------------------------------------------------
# reading stored settings back
# ---------------------------------------------------------------------------


def test_stored_settings_read_back_to_the_same_hash(tmp_path):
    variant = _variant(tmp_path, numpar=100)
    stored = dict(variant.run_settings)
    assert settings_hash(read_run_settings(stored)) == variant.result_hash

    # A field added later, with a default: an older record without it still matches.
    del stored["capemin"]
    assert settings_hash(read_run_settings(stored)) == variant.result_hash

    # A default that changed meaning: the stored value differs, so the run differs.
    stored["capemin"] = -2
    assert settings_hash(read_run_settings(stored)) != variant.result_hash


def _make_folder(directory, variant):
    """Make *variant*'s particles folder in the output *directory*, and return it."""
    out = Output(directory)
    out.write_log(variant, "202301011200_-111.85_40.77_5", "")
    return out.folder("particles", variant)


def _rewrite_record(folder, change) -> None:
    path = folder / "_settings.yaml"
    record = yaml.safe_load(path.read_text())
    change(record["settings"])
    path.write_text(yaml.safe_dump(record))


def test_a_folder_with_a_setting_this_version_lacks_still_loads(tmp_path):
    """
    A setting removed after a folder was written still loads, and keeps the folder apart.

    The run may have been made differently under it, so no variant is
    matched to the folder; it is listed as used by none.
    """
    variant = _variant(tmp_path, numpar=100)
    folder = _make_folder(tmp_path / "out", variant)

    def add_removed(settings):
        settings["removed_setting"] = 3
        settings["model"]["removed_too"] = "x"

    _rewrite_record(folder, add_removed)
    out = Output(tmp_path / "out")
    assert out.folder("particles", variant) is None
    listed = out.folders({"hrrr": variant})
    assert listed["folder"].tolist() == [folder.name.removeprefix("settings=")]
    assert listed["variant"].tolist() == [""]
    assert "removed_setting" in listed["differs"].iloc[0]


def test_a_build_detail_this_version_lacks_is_ignored(tmp_path):
    """The model's record is read through ``ModelInfo``, which drops what it does not have."""
    variant = _variant(tmp_path, numpar=100)
    folder = _make_folder(tmp_path / "out", variant)
    _rewrite_record(folder, lambda s: s["model"].update(removed_too="x"))
    assert Output(tmp_path / "out").folder("particles", variant) == folder


def test_output_finds_a_run_whose_stored_settings_predate_a_field(tmp_path):
    variant = _variant(tmp_path, numpar=100)
    run = _make_folder(tmp_path / "output", variant)
    _rewrite_record(run, lambda s: s.pop("capemin"))  # as if written before the field

    out = Output(tmp_path / "output")
    assert out.folder("particles", variant) == run
    renamed = replace(variant, name="hrrr-renamed")
    assert out.folder("particles", renamed) == run


# ---------------------------------------------------------------------------
# data files
# ---------------------------------------------------------------------------


def test_every_data_table_is_recorded_by_its_checksum(tmp_path):
    """No table is included, so a run records the four it read."""
    variant = _variant(tmp_path, numpar=100)
    assert set(variant.build.data_files) == {
        "ASCDATA.CFG",
        "LANDUSE.ASC",
        "ROUGLEN.ASC",
        "TERRAIN.ASC",
    }
    assert variant.run_settings["model"]["data_files"] == variant.build.data_files


def test_where_ascdata_cfg_says_the_tables_are_is_the_same_run(tmp_path):
    """HYSPLIT's ASCDATA.CFG says ../bdyfiles/; a run reads them where it links them, so the line changes nothing."""
    one = make_hysplit_dir(tmp_path / "one")
    two = make_hysplit_dir(tmp_path / "two")
    ascdata = two / "bdyfiles" / "ASCDATA.CFG"
    lines = ascdata.read_text().splitlines(keepends=True)
    lines[5] = "'./'  directory location of data files\n"
    ascdata.write_text("".join(lines))
    first = _variant(tmp_path, numpar=100, directory=one)
    second = _variant(tmp_path, numpar=100, directory=two)
    assert first.result_hash == second.result_hash


def test_a_changed_data_table_makes_another_run(tmp_path):
    """Another land use changes the particles."""
    one = make_hysplit_dir(tmp_path / "one")
    two = make_hysplit_dir(tmp_path / "two")
    (two / "bdyfiles" / "LANDUSE.ASC").write_text("other land use\n")
    first = _variant(tmp_path, numpar=100, directory=one)
    second = _variant(tmp_path, numpar=100, directory=two)
    assert first.build.sha256 == second.build.sha256
    differs = {
        name
        for name, digest in second.build.data_files.items()
        if first.build.data_files[name] != digest
    }
    assert differs == {"LANDUSE.ASC"}
    assert first.result_hash != second.result_hash


def test_without_directory_the_tables_beside_the_hycs_std_on_path_are_used(
    tmp_path, monkeypatch
):
    """A HYSPLIT folder whose exec/ is on PATH, as a cluster's HYSPLIT module sets it."""
    folder = make_hysplit_dir(tmp_path / "hysplit")
    terrain = folder / "bdyfiles" / "TERRAIN.ASC"
    terrain.write_text("other terrain\n")
    monkeypatch.setenv("PATH", str(folder / "exec"))
    variant = _variant(tmp_path, numpar=100)
    expected = hashlib.sha256(terrain.read_bytes()).hexdigest()
    assert variant.build.data_files["TERRAIN.ASC"] == expected


def test_a_hysplit_folder_without_its_tables_says_where_they_go(tmp_path):
    folder = make_hysplit_dir(tmp_path / "hysplit", tables=False)
    variant = _variant(tmp_path, numpar=100, directory=folder)
    with pytest.raises(HYSPLITNotFoundError, match="bdyfiles/ folder"):
        _ = variant.build


def test_a_file_stamp_says_which_folder_and_version_made_a_file():
    from runtraj.identity import file_stamp, runtraj_version

    assert file_stamp("abc123") == {
        b"runtraj:hash": b"abc123",
        b"runtraj:version": runtraj_version().encode(),
    }
    assert file_stamp("abc123", 2)[b"runtraj:realization"] == b"2"
    assert runtraj_version() == runtraj.__version__


@pytest.mark.parametrize(
    ("as_float", "as_int"),
    [
        ({"numpar": 1000.0}, {"numpar": 1000}),
        ({"levels": [0.0, 50.0]}, {"levels": [0, 50]}),
        ({"block": {"n_hours": -24.0}}, {"block": {"n_hours": -24}}),
    ],
)
def test_a_whole_number_float_and_its_integer_name_one_settings_folder(
    as_float, as_int
):
    assert settings_hash(as_float) == settings_hash(as_int)


def test_a_float_with_a_fraction_names_its_own_settings_folder():
    assert settings_hash({"numpar": 1000.5}) != settings_hash({"numpar": 1000})


# -- stored hashes do not move --------------------------------------------------


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            {"n_hours": -24},
            "07bd458bf04f35ec65ee75f559631d8d5bb3ae2ba05c19af11816ee5f55099af",
        ),
        (
            {"n_hours": -72, "numpar": 1000, "capemin": -1, "kmixd": 0},
            "8884f2627081a3ada6c6a78c3d536e512696bd45379a3e49d4ce49ef4b1ecf73",
        ),
    ],
)
def test_a_particles_folder_keeps_its_hash(config, expected):
    """The hash of HYSPLIT particles as stored since 0.1.0a2: a change that moves it orphans every folder."""
    info = ModelInfo(
        name="hysplit",
        sha256="0" * 64,
        data_files={"ASCDATA.CFG": "a" * 64},
        version="hysplit.v5.1.0",
    )
    met = {"source": "HRRR", "crop": None}
    record = run_settings(HysplitConfig(**config), met, info)
    assert settings_hash(record) == expected


def test_a_stage_folder_keeps_its_hash():
    """A stage without a version is hashed as before stages had versions."""
    assert (
        stage_hash("count", {"scale": 1}, "p" * 64)
        == "465f3905e5bfdcddb5237b547d176fc9b1b607d0811b92fbb241e455eecfc50c"
    )

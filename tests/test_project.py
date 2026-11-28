"""Tests for runtraj.project: the project directory, its tables, its results, and running it."""

import datetime as dt
from pathlib import Path

import matplotlib
import pandas as pd
import pytest

from runtraj import Project
from runtraj.config import ProjectConfig
from runtraj.models.hysplit import HysplitMetConfig
from runtraj.output import Output
from runtraj.releases import PointRelease

from .fixtures.factories import make_met_config, make_release
from .fixtures.platform import not_on_windows

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


def _met(tmp_path) -> HysplitMetConfig:
    return make_met_config(tmp_path / "met")


def _release(hour: int, longitude: float = -111.85, **attrs) -> PointRelease:
    return make_release(dt.datetime(2023, 1, 1, hour), longitude, **attrs)


def _project(tmp_path, releases=None, **settings) -> Project:
    """Make a project of particles only, whose met is one ARL file and whose output is ``tmp_path / "out"``."""
    settings.setdefault("variants", {"hrrr": {}})
    return Project.init(
        tmp_path / "proj",
        [_release(12), _release(13)] if releases is None else releases,
        mets={"hrrr": _met(tmp_path)},
        models={"hysplit": {"n_hours": -24}},
        output=str(tmp_path / "out"),
        **settings,
    )


def _settings(tmp_path) -> dict:
    """Return the settings of a project of particles only, as ``Project.init`` takes them."""
    return {
        "mets": {"hrrr": _met(tmp_path)},
        "models": {"hysplit": {"n_hours": -24}},
        "variants": {"hrrr": {}},
    }


def _run(project: Project, **kwargs) -> pd.DataFrame:
    return project.run(compute_root=project.directory.parent / "compute", **kwargs)


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
        models={"mine": {"class": "tests.models.test_models.ToyModel"}},
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


def test_init_takes_one_release_or_the_path_of_a_releases_csv(tmp_path, point_release):
    one = Project.init(tmp_path / "one", point_release, **_settings(tmp_path))
    from_csv = Project.init(tmp_path / "copy", one.releases_path, **_settings(tmp_path))

    assert list(one.releases["release"]) == [point_release.id]
    assert list(from_csv.releases["release"]) == [point_release.id]


def test_init_refuses_releases_that_are_not_releases(tmp_path):
    with pytest.raises(TypeError, match="Releases must be a Release"):
        Project.init(tmp_path / "proj", ["not a release"], **_settings(tmp_path))
    assert not (tmp_path / "proj").exists()


def test_init_refuses_an_empty_list_of_releases_and_writes_nothing(tmp_path):
    with pytest.raises(ValueError, match="No releases were given"):
        Project.init(tmp_path / "proj", [], **_settings(tmp_path))
    assert not (tmp_path / "proj").exists()


def test_init_takes_a_config_or_settings_not_both(tmp_path):
    config = ProjectConfig(**_settings(tmp_path))
    with pytest.raises(TypeError, match="not both"):
        Project.init(tmp_path / "proj", config=config, output="elsewhere")


def test_init_takes_output_as_a_path_like_as_a_string(tmp_path):
    from_str = Project.init(
        tmp_path / "a", **_settings(tmp_path), output=str(tmp_path / "out")
    )
    from_path = Project.init(
        tmp_path / "b", **_settings(tmp_path), output=tmp_path / "out"
    )

    assert from_path.config.output == from_str.config.output == str(tmp_path / "out")
    assert from_path.output.directory == from_str.output.directory


def test_init_still_takes_output_as_a_url(tmp_path):
    project = Project.init(
        tmp_path / "proj", **_settings(tmp_path), output="s3://bucket/output"
    )

    assert project.config.output == "s3://bucket/output"


def test_init_refuses_a_directory_that_is_already_a_project(tmp_path):
    project = _project(tmp_path)
    before = project.config_path.read_text()

    with pytest.raises(FileExistsError, match="already has a config.yaml"):
        Project.init(project.directory, **_settings(tmp_path))
    assert project.config_path.read_text() == before


def test_a_project_without_releases_has_no_simulations(tmp_path):
    project = Project.init(tmp_path / "proj", **_settings(tmp_path))

    assert not project.releases_path.exists()
    assert project.releases.empty
    assert project.simulations.empty
    assert list(project.simulations.columns[:4]) == [
        "release",
        "variant",
        "realization",
        "model",
    ]


def test_a_receptors_csv_is_named_as_renamed(tmp_path):
    project = _project(tmp_path)
    project.releases_path.rename(project.directory / "receptors.csv")

    with pytest.raises(ValueError, match="now named releases.csv"):
        Project(project.directory)


# ---------------------------------------------------------------------------
# Output directory and scratch
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


def test_add_releases_appends_the_new_ones_and_returns_their_ids(tmp_path):
    project = _project(tmp_path, [_release(12)])
    assert len(project.simulations) == 1

    added = project.add_releases([_release(12), _release(14), _release(14)])

    assert added == [_release(14).id]
    assert list(project.releases["release"]) == [_release(12).id, _release(14).id]
    assert len(project.simulations) == 2  # read again from the file
    assert Project(project.directory).releases.equals(project.releases)
    assert project.add_releases(_release(14)) == []


def test_add_releases_that_stops_part_way_leaves_releases_csv_as_it_was(
    tmp_path, monkeypatch
):
    project = _project(tmp_path, [_release(12)])
    before = project.releases_path.read_text(encoding="utf-8")
    write_text = Path.write_text

    def stops_part_way(path, text, *args, **kwargs):
        write_text(path, text[: len(text) // 2], *args, **kwargs)
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "write_text", stops_part_way)
    with pytest.raises(OSError, match="No space"):
        project.add_releases([_release(14)])
    monkeypatch.undo()

    assert project.releases_path.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.rglob("*.tmp")] == []


@not_on_windows("a symbolic link needs extra rights on Windows")
def test_add_releases_writes_through_a_linked_releases_csv(tmp_path):
    project = _project(tmp_path, [_release(12)])
    shared = tmp_path / "shared.csv"
    project.releases_path.rename(shared)
    project.releases_path.symlink_to(shared)

    project.add_releases([_release(14)])

    assert project.releases_path.is_symlink()
    assert len(Project(project.directory).releases) == 2
    assert shared.read_text(encoding="utf-8").count("\n") == 3


def test_a_release_the_project_does_not_have_is_a_key_error(tmp_path):
    project = _project(tmp_path)

    assert project.release(_release(12).id) == _release(12)
    with pytest.raises(KeyError, match="No release 'nope'"):
        project.release("nope")


# ---------------------------------------------------------------------------
# Variants and simulations
# ---------------------------------------------------------------------------


def test_simulations_are_every_release_under_every_variant(tmp_path):
    project = _project(tmp_path, variants={"hrrr": {}, "long": {"n_hours": -48}})

    sims = project.simulations

    assert list(sims.columns[:4]) == ["release", "variant", "realization", "model"]
    assert list(sims["variant"]) == ["hrrr", "long", "hrrr", "long"]
    assert list(sims["release"]) == [_release(12).id] * 2 + [_release(13).id] * 2
    assert sims["realization"].isna().all()
    assert set(sims["model"]) == {"hysplit"}


def test_simulations_is_a_copy(tmp_path):
    project = _project(tmp_path)

    sims = project.simulations
    sims.loc[0, "variant"] = "changed"

    assert list(project.simulations["variant"]) == ["hrrr", "hrrr"]


def test_an_ensemble_has_a_row_for_each_realization(tmp_path):
    project = _project(
        tmp_path, [_release(12)], variants={"ens": {"realizations": 3, "krand": 4}}
    )

    sims = project.simulations

    assert list(sims["realization"]) == [0, 1, 2]
    assert str(sims["realization"].dtype) == "Int64"


def test_a_simulation_of_a_variant_the_project_does_not_have_is_a_key_error(tmp_path):
    project = _project(tmp_path)

    with pytest.raises(
        KeyError, match=r"No variant 'nope'; the variants are \['hrrr'\]"
    ):
        project.simulation(_release(12).id, "nope")


# ---------------------------------------------------------------------------
# Completion and status
# ---------------------------------------------------------------------------


def test_a_simulation_is_pending_until_its_particles_are_written(
    tmp_path, transport_runs
):
    project = _project(tmp_path)
    assert list(project.status()["state"]) == ["pending", "pending"]
    assert len(project.incomplete()) == 2

    _run(project)

    status = project.status()
    assert list(status.columns[-5:]) == [
        "particles",
        "state",
        "stage",
        "reason",
        "message",
    ]
    assert list(status["state"]) == ["complete", "complete"]
    assert status["particles"].tolist() == [True, True]
    assert status[["stage", "reason", "message"]].isna().all().all()
    assert project.incomplete().empty


def test_a_failed_run_says_why(tmp_path, transport_runs):
    project = _project(tmp_path)
    transport_runs.fail[_release(13).id] = "MET_COVERAGE"

    _run(project)

    status = project.status().set_index("release")
    failed = status.loc[_release(13).id]
    assert failed["state"] == "failed"
    assert not failed["particles"]
    assert failed["stage"] == "particles"
    assert failed["reason"] == "MET_COVERAGE"
    assert "the run stopped" in failed["message"]
    assert status.loc[_release(12).id, "state"] == "complete"
    assert list(project.incomplete()["release"]) == [_release(13).id]


def test_a_run_that_left_a_log_and_no_particles_was_interrupted(tmp_path):
    project = _project(tmp_path)
    sim = project.simulation(_release(12).id, "hrrr")
    project.output.write_log(sim.variant, sim.release.id, "started\n")

    status = project.status().set_index("release")

    assert status.loc[_release(12).id, "state"] == "interrupted"
    assert status.loc[_release(13).id, "state"] == "pending"


def test_a_failure_is_cleared_when_a_run_succeeds(tmp_path, transport_runs):
    project = _project(tmp_path, [_release(12)])
    transport_runs.fail[_release(12).id] = "MET_COVERAGE"
    _run(project)
    assert list(project.status()["state"]) == ["failed"]

    transport_runs.fail.clear()
    _run(project)

    status = project.status()
    assert list(status["state"]) == ["complete"]
    assert status["reason"].isna().all()


def test_variants_with_equal_run_settings_share_one_run(tmp_path, transport_runs):
    project = _project(tmp_path, [_release(12)], variants={"a": {}, "b": {}})

    _run(project)

    assert transport_runs.ran == [_release(12).id]
    assert list(project.status()["state"]) == ["complete", "complete"]


def test_a_run_skips_what_is_complete_unless_told_not_to(tmp_path, transport_runs):
    project = _project(tmp_path)
    _run(project)
    assert len(transport_runs.ran) == 2

    again = _run(project)
    assert len(transport_runs.ran) == 2
    assert again.empty

    _run(project, skip_existing=False)
    assert len(transport_runs.ran) == 4


def test_folders_lists_the_settings_folder_of_each_kind(tmp_path, transport_runs):
    project = _project(tmp_path)
    assert project.folders().empty

    _run(project)

    folders = project.folders()
    assert list(folders["kind"]) == ["particles"]
    assert folders["folder"].iloc[0].startswith("hrrr-")
    assert folders["name"].iloc[0] == "hrrr"
    assert folders["files"].iloc[0] == 2
    assert folders["variant"].iloc[0] == "hrrr"


def test_a_folder_no_variant_uses_says_how_it_differs(tmp_path, transport_runs):
    project = _project(tmp_path, [_release(12)])
    _run(project)
    text = project.config_path.read_text().replace("n_hours: -24", "n_hours: -12")
    project.config_path.write_text(text)

    folders = Project(project.directory).folders()

    assert len(folders) == 1
    assert folders["variant"].iloc[0] == ""
    assert "n_hours" in folders["differs"].iloc[0]


# ---------------------------------------------------------------------------
# Loading results
# ---------------------------------------------------------------------------


def test_particles_are_one_table_with_the_simulation_first(tmp_path, transport_runs):
    project = _project(tmp_path)
    _run(project)

    particles = project.particles()

    assert list(particles.columns[:3]) == ["release", "variant", "realization"]
    assert list(dict.fromkeys(particles["release"])) == [
        _release(12).id,
        _release(13).id,
    ]
    assert set(particles["variant"]) == {"hrrr"}
    assert particles["realization"].isna().all()
    assert len(particles) == 2 * 5 * 10  # 5 particles, 10 steps each
    assert pd.api.types.is_datetime64_any_dtype(particles["time"])


def test_particles_can_be_limited_to_some_columns(tmp_path, transport_runs):
    project = _project(tmp_path)
    _run(project)

    some = project.particles(columns=["particle", "lon"])
    timed = project.particles(columns=["time", "lat"])

    assert list(some.columns) == [
        "release",
        "variant",
        "realization",
        "particle",
        "lon",
    ]
    # time is built from age, which is read for it and not returned.
    assert list(timed.columns) == ["release", "variant", "realization", "time", "lat"]


def test_particles_leave_out_simulations_without_them(tmp_path, transport_runs):
    project = _project(tmp_path)
    assert list(project.particles().columns) == ["release", "variant", "realization"]
    transport_runs.fail[_release(13).id] = "MET_COVERAGE"
    _run(project)

    particles = project.particles()

    assert set(particles["release"]) == {_release(12).id}


def test_variants_that_share_a_run_each_get_its_particles(tmp_path, transport_runs):
    project = _project(tmp_path, [_release(12)], variants={"a": {}, "b": {}})
    _run(project)

    particles = project.particles()

    assert list(dict.fromkeys(particles["variant"])) == ["a", "b"]
    a, b = (particles[particles["variant"] == v].drop(columns="variant") for v in "ab")
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))


def test_an_ensembles_particles_are_one_set_per_realization(tmp_path, transport_runs):
    project = _project(
        tmp_path, [_release(12)], variants={"ens": {"realizations": 2, "krand": 4}}
    )
    _run(project)

    particles = project.particles()

    assert list(project.status()["state"]) == ["complete", "complete"]
    assert particles["realization"].value_counts().to_dict() == {0: 50, 1: 50}


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


def test_run_runs_the_releases_it_is_given_in_this_process(tmp_path, ran):
    project = _project(tmp_path)

    table = _run(project, releases=[_release(13).id])

    assert [call["ids"] for call in ran] == [[_release(13).id]]
    assert list(table["release"]) == [_release(13).id]
    assert list(table["state"]) == ["pending"]  # the fake ran nothing


@pytest.mark.parametrize("method", ["run", "submit"])
def test_run_and_submit_take_their_options_by_keyword_only(tmp_path, ran, method):
    """Release ids passed first are not read as ``skip_existing`` (which ran the whole project)."""
    project = _project(tmp_path)

    with pytest.raises(TypeError, match="positional"):
        getattr(project, method)([_release(12).id])
    assert ran == []


def test_run_refuses_a_release_the_project_does_not_have(tmp_path, ran):
    project = _project(tmp_path)

    with pytest.raises(ValueError, match="nope"):
        _run(project, releases=["nope"])
    assert ran == []


def test_submit_needs_the_slurm_backend(tmp_path):
    project = _project(tmp_path)

    with pytest.raises(ValueError, match="submit sends work to a scheduler"):
        project.submit()


def test_run_on_slurm_submits_and_waits(tmp_path, monkeypatch):
    project = _project(
        tmp_path,
        [_release(12)],
        execution={"backend": "slurm", "slurm": {"time": "01:00:00"}},
    )
    submitted, waited, slept = [], [], []

    def submit(project, pending, execution, skip_existing, compute_root):
        submitted.append(
            {"pending": pending, "execution": execution, "compute_root": compute_root}
        )
        return "777", "kingspeak"

    def wait(job_id, *, cluster=None):
        waited.append((job_id, cluster))

    monkeypatch.setattr("runtraj.execution.slurm.submit_array", submit)
    monkeypatch.setattr("runtraj.execution.slurm.wait", wait)
    monkeypatch.setattr("runtraj.execution.runner.time.sleep", slept.append)

    status = project.run(compute_root="/s")

    assert waited == [("777", "kingspeak")]
    assert slept == [60]  # nothing ran, so it listed once more before saying so
    assert submitted[0]["pending"] == [_release(12).id]
    assert submitted[0]["compute_root"] == "/s"
    assert submitted[0]["execution"].backend == "slurm"
    # The status of what was submitted; nothing ran here.
    assert list(status["release"]) == [_release(12).id]
    assert list(status["state"]) == ["pending"]


# ---------------------------------------------------------------------------
# A selection of simulations
# ---------------------------------------------------------------------------


@pytest.fixture
def selecting(tmp_path):
    """A project of two releases under two variants, with nothing run."""
    return _project(tmp_path, variants={"hrrr": {}, "long": {"n_hours": -48}})


def test_a_selection_is_rows_of_simulations(selecting):
    sims = selecting.simulations

    status = selecting.status(sims[sims["variant"] == "long"])

    assert list(status["variant"]) == ["long", "long"]
    assert list(status["release"]) == [_release(12).id, _release(13).id]


def test_a_selection_is_a_mask_over_simulations(selecting):
    sims = selecting.simulations
    mask = sims["release"] == _release(13).id

    by_series = selecting.status(mask)
    by_array = selecting.incomplete(mask.to_numpy())
    by_list = selecting.status(mask.tolist())

    for table in (by_series, by_array, by_list):
        assert set(table["release"]) == {_release(13).id}
        assert list(table["variant"]) == ["hrrr", "long"]


@pytest.mark.parametrize("dtype", ["boolean", "bool[pyarrow]"])
def test_a_mask_with_missing_values_selects_nothing_there(selecting, dtype):
    sims = selecting.simulations
    mask = pd.Series(
        [pd.NA, True, False, pd.NA][: len(sims)], dtype=dtype, index=sims.index
    )

    status = selecting.status(mask)

    assert len(status) == 1
    assert status["release"].iloc[0] == sims["release"].iloc[1]
    assert status["variant"].iloc[0] == sims["variant"].iloc[1]


def test_a_complete_pyarrow_mask_is_a_mask(selecting):
    sims = selecting.simulations
    mask = (sims["release"] == _release(13).id).astype("bool[pyarrow]")

    assert set(selecting.status(mask)["release"]) == {_release(13).id}


def test_a_selection_is_any_table_with_release_and_variant_columns(selecting):
    import pyarrow as pa

    table = pa.table({"release": [_release(12).id], "variant": ["long"]})
    extra = pd.DataFrame(
        {"release": [_release(12).id], "variant": ["long"], "note": ["x"]}
    )

    status = selecting.status(table)
    kept = selecting.status(extra)

    assert list(status["variant"]) == ["long"]
    assert "model" in status.columns  # the rest of the row, from simulations
    assert list(kept["note"]) == ["x"]


def test_a_table_without_realization_selects_every_realization(tmp_path):
    project = _project(
        tmp_path, [_release(12)], variants={"ens": {"realizations": 2, "krand": 4}}
    )

    status = project.status(
        pd.DataFrame({"release": [_release(12).id], "variant": ["ens"]})
    )

    assert list(status["realization"]) == [0, 1]


@pytest.mark.parametrize(
    ("sel", "error", "match"),
    [
        (pd.DataFrame({"release": ["r"]}), ValueError, r"has no \['variant'\]"),
        (
            pd.DataFrame({"receptor": ["r"], "variant": ["hrrr"]}),
            ValueError,
            "now named 'release'",
        ),
        ("hrrr", ValueError, "a mask over project.simulations"),
        (
            pd.DataFrame({"release": ["r"], "variant": ["nope"]}),
            KeyError,
            r"No variant \['nope'\]",
        ),
    ],
)
def test_a_selection_that_is_not_one_is_refused(selecting, sel, error, match):
    with pytest.raises(error, match=match):
        selecting.status(sel)


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


def test_a_table_is_kept_as_parquet_and_added_to(tmp_path):
    project = _project(tmp_path)
    first = pd.DataFrame({"release": ["a", "b"], "weight": [1.0, 2.0]})

    path = project.add_table("kernels", first)
    project.add_table(
        "kernels", pd.DataFrame({"release": ["b", "c"], "weight": [9.0, 3.0]})
    )

    assert path == project.directory / "tables" / "kernels.parquet"
    assert path == project.table_path("kernels")
    held = pd.read_parquet(path)
    # Rows of a release the table holds are left out.
    assert list(held["release"]) == ["a", "b", "c"]
    assert list(held["weight"]) == [1.0, 2.0, 3.0]


def test_a_table_without_a_release_column_keeps_every_row(tmp_path):
    project = _project(tmp_path)
    rows = pd.DataFrame({"level": [1, 2]})

    project.add_table("levels", rows)
    project.add_table("levels", rows)

    assert list(pd.read_parquet(project.table_path("levels"))["level"]) == [1, 2, 1, 2]


def test_a_table_refuses_other_columns_and_a_bad_name(tmp_path):
    project = _project(tmp_path)
    project.add_table("kernels", pd.DataFrame({"release": ["a"], "weight": [1.0]}))

    with pytest.raises(ValueError, match="has the columns"):
        project.add_table("kernels", pd.DataFrame({"release": ["b"], "w": [1.0]}))
    with pytest.raises(ValueError, match="A table name is letters"):
        project.table_path("../kernels")


def test_a_table_written_with_a_receptor_column_is_named_as_renamed(tmp_path):
    project = _project(tmp_path)
    path = project.table_path("kernels")
    path.parent.mkdir()
    pd.DataFrame({"receptor": ["a"], "weight": [1.0]}).to_parquet(path)

    with pytest.raises(ValueError, match="now named 'release'"):
        project.add_table("kernels", pd.DataFrame({"release": ["b"], "weight": [1.0]}))


@pytest.mark.parametrize("keep_workdir", [False, True])
def test_a_failed_runs_kept_workdir_leaves_out_its_particle_file(
    tmp_path, monkeypatch, keep_workdir
):
    from runtraj.exceptions import SimulationError
    from runtraj.execution import ExecutionConfig
    from runtraj.models.hysplit.driver import PARTICLE_FILE

    def fail(model, release, transport, met, workdir, **kwargs):
        (workdir / "CONTROL").write_text("inputs")
        (workdir / PARTICLE_FILE).write_text("a partial particle file")
        raise SimulationError("the run stopped", reason="MET_COVERAGE")

    monkeypatch.setattr("runtraj.execution.worker.run_model", fail)
    project = _project(tmp_path, [_release(12)])
    _run(project, execution=ExecutionConfig(keep_workdir=keep_workdir))

    kept = project.simulation(_release(12).id, "hrrr").kept_workdir
    assert (kept / "CONTROL").read_text() == "inputs"
    assert (kept / PARTICLE_FILE).exists() == keep_workdir

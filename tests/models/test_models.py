"""Tests for a transport model as a variant axis: a variant may run another model."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import xarray as xr
import yaml
from pydantic import BaseModel

from runtraj import models
from runtraj.config import ProjectConfig
from runtraj.identity import read_run_settings, settings_hash
from runtraj.models import MetConfig, ModelInfo, TransportConfig, TransportModel
from runtraj.spatial import Bounds

from ..fixtures.factories import make_met_config
from ..fixtures.models import TOY, ToyConfig, ToyMet, ToyModel

#: The repository, whose ``tests`` package holds the toy model.
REPOSITORY = Path(__file__).parents[2]

GRID = {"xmin": -112, "xmax": -111, "ymin": 40, "ymax": 41, "xres": 0.1, "yres": 0.1}


@pytest.fixture
def toy(monkeypatch):
    monkeypatch.setitem(models.MODELS, "toy", ToyModel)


def _mets(tmp_path):
    return {"hrrr": make_met_config(tmp_path / "met")}


def test_each_models_block_holds_its_own_defaults(tmp_path, toy):
    """A variant starts from its own model's block: nothing passes from one model to another."""
    config = ProjectConfig(
        mets=_mets(tmp_path),
        models={
            "hysplit": {"n_hours": -6, "numpar": 300, "krand": 2},
            "toy": {"n_hours": -12, "n_particles": 7},
        },
        variants={
            "hrrr": {"model": "hysplit"},
            "toy": {"model": "toy", "met": "hrrr"},
            "toy-long": {"model": "toy", "met": "hrrr", "n_hours": -48},
        },
    )
    variants = config.resolve()
    assert variants["hrrr"].transport.n_hours == -6
    assert variants["toy"].transport.n_hours == -12
    assert variants["toy"].transport.n_particles == 7
    assert variants["toy-long"].transport.n_hours == -48
    assert "numpar" not in type(variants["toy"].transport).model_fields
    for hysplits_own in ("krand", "numpar"):
        with pytest.raises(ValueError, match=f"'{hysplits_own}' is not a setting"):
            ProjectConfig(
                mets=_mets(tmp_path),
                models={"toy": {}},
                variants={"hrrr": {hysplits_own: 2}},
            )
        with pytest.raises(
            ValueError, match=f"models: toy: '{hysplits_own}' is not a setting"
        ):
            ProjectConfig(
                mets=_mets(tmp_path),
                models={"toy": {hysplits_own: 2}},
                variants={"hrrr": {}},
            )


def test_a_variant_names_its_model_when_there_are_several(tmp_path, toy):
    models = {"hysplit": {"n_hours": -24}, "toy": {"n_hours": -24}}
    with pytest.raises(
        ValueError,
        match=r"Variant 'hrrr' must name its model \(one of \['hysplit', 'toy'\]\)",
    ):
        ProjectConfig(mets=_mets(tmp_path), models=models, variants={"hrrr": {}})
    with pytest.raises(
        ValueError, match=r"names the model 'nope', which models: does not have"
    ):
        ProjectConfig(
            mets=_mets(tmp_path), models=models, variants={"hrrr": {"model": "nope"}}
        )
    # With one model, a variant need not say it.
    only = ProjectConfig(
        mets=_mets(tmp_path), models={"toy": {"n_hours": -24}}, variants={"hrrr": {}}
    )
    assert only.resolve()["hrrr"].model == "toy"


def test_a_met_is_checked_by_the_model_that_reads_it(tmp_path, toy):
    """``met_config`` takes the name of a models block, and needs it when there are several."""
    one = ProjectConfig(
        mets=_mets(tmp_path), models={"toy": {"n_hours": -24}}, variants={"hrrr": {}}
    )
    assert isinstance(one.met_config("hrrr"), ToyMet)  # the only model

    both = ProjectConfig(
        mets=_mets(tmp_path),
        models={"hysplit": {"n_hours": -24}, "toy": {"n_hours": -24}},
        variants={"hrrr": {"model": "hysplit"}},
    )
    assert isinstance(both.met_config("hrrr", "toy"), ToyMet)
    assert not isinstance(both.met_config("hrrr", "hysplit"), ToyMet)
    with pytest.raises(
        ValueError,
        match=r"Name the model that reads met 'hrrr': one of \['hysplit', 'toy'\]",
    ):
        both.met_config("hrrr")
    with pytest.raises(ValueError, match="There is no model 'nope' under models:"):
        both.met_config("hrrr", "nope")


def test_with_several_models_a_met_no_variant_uses_is_not_checked(tmp_path, toy):
    """No variant pairs the spare met with a model, so there is no met config to check it with."""
    mets = {**_mets(tmp_path), "spare": {"subgrid_enable": "maybe"}}
    config = ProjectConfig(
        mets=mets,
        models={"hysplit": {"n_hours": -24}, "toy": {"n_hours": -24}},
        variants={"hrrr": {"model": "hysplit"}},
    )
    # HYSPLIT's met config refuses it once it is asked.
    with pytest.raises(ValueError, match=r"Met 'spare' \(hysplit\)"):
        config.met_config("spare", "hysplit")


def test_a_models_block_must_name_a_model(tmp_path, toy):
    with pytest.raises(
        ValueError, match="models: nope: 'nope' is not a built-in transport model"
    ):
        ProjectConfig(mets=_mets(tmp_path), models={"nope": {}}, variants={"hrrr": {}})
    with pytest.raises(ValueError, match="class must be the import path"):
        ProjectConfig(
            mets=_mets(tmp_path),
            models={"mine": {"class": "toy"}},
            variants={"hrrr": {}},
        )
    with pytest.raises(ValueError, match="names no transport model"):
        ProjectConfig(mets=_mets(tmp_path), models={}, variants={"hrrr": {}})


def test_a_model_has_one_block(tmp_path):
    """Two blocks that name one class are refused: the second set of settings is a variant."""
    with pytest.raises(
        ValueError,
        match=f"models: a and b are both {TOY}. A model has one block",
    ):
        ProjectConfig(
            mets=_mets(tmp_path),
            models={"a": {"class": TOY}, "b": {"class": TOY}},
            variants={"hrrr": {"model": "a"}},
        )


def test_a_config_without_models_is_refused_naming_the_built_in_models(tmp_path):
    """A config without models: is refused, since there is no default transport model."""
    with pytest.raises(ValueError, match="names no transport model") as refused:
        ProjectConfig(mets=_mets(tmp_path), variants={"hrrr": {"n_hours": -24}})
    assert "flexpart, hysplit" in str(refused.value)
    assert "class:" in str(refused.value)
    path = _write(
        tmp_path, f"mets:\n  hrrr: {tmp_path / 'met'}\nvariants:\n  hrrr: {{}}\n"
    )
    with pytest.raises(ValueError, match="names no transport model"):
        ProjectConfig.from_yaml(path)


def test_a_models_block_is_written_back_as_given(tmp_path):
    given = ProjectConfig(
        mets=_mets(tmp_path),
        models={"hysplit": {"n_hours": -24, "numpar": 300, "seed": 4, "krand": 2}},
        variants={"hrrr": {}},
    )
    text = given.to_yaml()
    assert yaml.safe_load(text)["models"] == {
        "hysplit": {"n_hours": -24, "numpar": 300, "seed": 4, "krand": 2}
    }
    loaded = ProjectConfig.from_yaml(_write(tmp_path, text))
    assert loaded.resolve()["hrrr"].transport.seed == 4


def test_an_empty_models_block_runs_the_models_defaults(tmp_path):
    """``hysplit:`` with nothing under it is read as no settings."""
    path = _write(
        tmp_path,
        f"mets:\n  hrrr: {tmp_path / 'met'}\nmodels:\n  hysplit:\n"
        "variants:\n  hrrr: {n_hours: -24}\n",
    )
    variant = ProjectConfig.from_yaml(path).resolve()["hrrr"]
    assert variant.transport == type(variant.transport)(n_hours=-24)


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_a_model_config_inherits_the_recorded_settings_and_realizations():
    config = ToyConfig(n_hours=-6, seed=5, n_particles=10, label="a note")

    assert config.settings() == {
        "n_hours": -6,
        "seed": 5,
        "n_particles": 10,
        "step": 60,
    }
    assert [r.seed for r in config.realizations(3)] == [5, 6, 7]
    assert [r.seed for r in ToyConfig(n_hours=-6).realizations(2)] == [None, None]
    with pytest.raises(ValueError, match="extra"):
        ToyConfig(n_hours=-6, unknown=1)
    with pytest.raises(ValueError, match="n_hours"):
        ToyConfig()  # a config must say how long the run is


def test_a_model_is_named_by_its_import_path_without_registering_it(tmp_path):
    """The block's name is the variant's handle; ``class`` is what runs and what is recorded."""
    config = ProjectConfig(
        mets=_mets(tmp_path),
        models={"mine": {"class": TOY, "n_hours": -6, "n_particles": 3}},
        variants={"toy": {"model": "mine", "met": "hrrr"}},
    )
    variant = config.resolve()["toy"]
    assert variant.transport.n_particles == 3
    assert yaml.safe_load(config.to_yaml())["models"] == {
        "mine": {"class": TOY, "n_hours": -6, "n_particles": 3}
    }
    # The record names the import path, so another process imports it again.
    assert variant.build == ModelInfo(name=TOY, version="toy-1")
    stored = read_run_settings(variant.run_settings)
    assert settings_hash(stored) == variant.result_hash


def test_a_record_of_a_model_not_installed_still_reads(tmp_path):
    """Another project's folder in a shared output must not take status down."""
    stored = {
        "n_hours": -6,
        "nparticles": 3,
        "model": {"name": "nopkg.models.Missing", "version": "1.0"},
        "met": {"source": "toy"},
    }
    with pytest.warns(UserWarning, match="could not be loaded"):
        assert read_run_settings(stored) == stored


def test_a_model_in_its_own_package_runs_from_a_fresh_interpreter(tmp_path):
    """No registration in the process: the import path alone finds the model."""
    script = textwrap.dedent(
        f"""
        import runtraj
        release = runtraj.PointRelease(
            time="2023-01-01 12:00", longitude=-111.85, latitude=40.77, altitude=5
        )
        met = {{"weather": "hrrr"}}  # no directory: the toy reads no met files
        particles = runtraj.run_trajectories(
            release, met, model={TOY!r}, n_hours=-2, n_particles=3
        )
        print(len(particles), sorted(int(p) for p in particles["particle"].unique()))
        """
    )
    done = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(REPOSITORY)!r})\n{script}",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "6 [10, 20, 30]"


# -- MetConfig: a met and what is done with its files -------------------------


class _NoFiles(MetConfig):
    """A met that names no files: every method but settings is the default."""

    weather: str = "built-in"

    def settings(self):
        return {"weather": self.weather}


class _Grib(_NoFiles):
    """A met whose files are not ARL files: it opens them itself."""

    def open(self, files, bounds=None):
        return xr.Dataset(attrs={"files": list(files), "bounds": bounds})


def test_a_met_keeps_the_defaults_it_does_not_override():
    met = _NoFiles()
    checked = met.check(None, -24)
    assert checked.empty and list(checked.columns) == ["check", "release", "message"]
    assert met.fetch([]).empty and list(met.fetch([]).columns) == ["file", "message"]
    assert met.downloads() is False
    assert met.open(["a.arl", "b.arl"]) == [Path("a.arl"), Path("b.arl")]


def test_a_met_that_opens_its_files_returns_a_dataset():
    bounds = Bounds(xmin=-113, xmax=-111, ymin=40, ymax=41)
    opened = _Grib().open((f for f in ["a.grb2"]), bounds)
    assert opened.attrs == {"files": ["a.grb2"], "bounds": bounds}


def test_a_met_without_settings_cannot_be_made():
    class NoSettings(MetConfig):
        pass

    with pytest.raises(
        TypeError, match="abstract method 'settings'|abstract method settings"
    ):
        NoSettings()


# -- the TransportModel base class ---------------------------------------------


class _Defaults(TransportModel):
    """A transport model that overrides nothing it need not."""

    name = "defaults"

    def run(self, release, config, met, window, workdir=None, timeout=None):
        raise NotImplementedError


def test_a_model_keeps_the_defaults_it_does_not_override():
    model = _Defaults()
    assert (model.needs_workdir, model.batched) == (True, False)
    assert model.result(None) == "particles"
    assert model.result_files(None) == ()
    assert (model.sha256(None), model.version(None), model.data_files(None)) == (
        None,
        None,
        None,
    )
    assert model.release_rows(None, None) is None
    table = model.check_releases(None, None)
    assert table.empty and list(table.columns) == ["check", "release", "message"]
    with pytest.raises(NotImplementedError, match="sets batched but has no run_many"):
        model.run_many([], None, None, [])


class _NoRun(TransportModel):
    name = "norun"
    config_class = ToyConfig
    met_config_class = ToyMet


def test_get_model_refuses_a_model_without_run():
    with pytest.raises(ValueError, match="is not a transport model: it has no run"):
        models.get_model("tests.models.test_models._NoRun")


def test_check_model_reports_a_name_spelled_almost_like_a_hook():
    class Typo(ToyModel):
        def release_row(self, particles, release):
            return None

    with pytest.raises(AssertionError, match="Typo.release_row is spelled almost"):
        models.check_model(Typo(), ToyConfig(n_hours=-1))


def test_get_model_refuses_a_class_that_is_not_a_transport_model():
    with pytest.raises(ValueError, match="must subclass runtraj.models.TransportModel"):
        models.get_model("tests.fixtures.models.ToyConfig")


def test_get_model_refuses_a_met_config_that_is_not_a_met_config(monkeypatch):
    import sys
    import types

    class PlainMet(_Defaults):
        config_class = TransportConfig
        met_config_class = BaseModel

    module = types.ModuleType("plainmet_for_this_test")
    module.PlainMet = PlainMet  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "plainmet_for_this_test", module)
    with pytest.raises(
        ValueError, match="met_config_class must subclass runtraj.models.MetConfig"
    ):
        models.get_model("plainmet_for_this_test.PlainMet")

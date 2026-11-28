"""Tests for check_model: the promises a transport model in another package keeps."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from runtraj import ColumnRelease, MultiPointRelease, PointRelease
from runtraj.exceptions import SimulationError
from runtraj.models import ModelRun, TransportConfig
from runtraj.models.hysplit import HysplitConfig, HysplitModel
from runtraj.testing import check_model

from ..fixtures.models import ToyConfig, ToyMet, ToyModel

NOON = dt.datetime(2023, 1, 1, 12)

RELEASES = [
    PointRelease(time=NOON, longitude=-111.85, latitude=40.77, altitude=5),
    ColumnRelease(time=NOON, longitude=-111.85, latitude=40.77, bottom=0, top=100),
    MultiPointRelease(
        time=NOON,
        longitudes=[-111.85, -111.85, -111.80],
        latitudes=[40.77, 40.77, 40.80],
        altitudes=[5.0, 50.0, 20.0],
    ),
]


@pytest.mark.parametrize("release", RELEASES, ids=lambda r: r.kind)
@pytest.mark.parametrize("n_hours", [-3, 3])
def test_the_toy_model_keeps_its_promises(release, n_hours):
    check_model(ToyModel(), ToyConfig(n_hours=n_hours, seed=1), ToyMet(), release)


def test_hysplits_config_keeps_its_promises():
    check_model(HysplitModel(), HysplitConfig(n_hours=-24, numpar=50, seed=3, krand=2))


class _Unreadable(ToyConfig):
    """Records a setting its class cannot read back."""

    def settings(self) -> dict[str, Any]:
        return {**super().settings(), "n_particles": "many"}


class _Opaque(ToyConfig):
    """Records a value a settings file cannot hold."""

    def settings(self) -> dict[str, Any]:
        return {**super().settings(), "when": NOON}


@pytest.mark.parametrize(
    ("config_class", "message"),
    [
        (_Unreadable, "cannot read back"),
        (_Opaque, "plain data"),
    ],
)
def test_a_config_whose_record_does_not_read_back_fails(config_class, message):
    class Model(ToyModel):
        pass

    Model.config_class = config_class
    with pytest.raises(AssertionError, match=message):
        check_model(Model(), config_class(n_hours=-3))


class _Forward(ToyModel):
    """Writes positive ages for a backward run."""

    def run(self, release, config, met, window, workdir=None, timeout=None):
        run = super().run(release, config, met, window, workdir, timeout)
        return ModelRun(run.result.assign(age=run.result["age"].abs()))


class _TooLong(ToyModel):
    """Writes rows past the end of the run."""

    def run(self, release, config, met, window, workdir=None, timeout=None):
        run = super().run(release, config, met, window, workdir, timeout)
        return ModelRun(run.result.assign(age=run.result["age"] * 2))


class _NoReleaseRows(ToyModel):
    release_rows = None


class _NoLon(ToyModel):
    def run(self, release, config, met, window, workdir=None, timeout=None):
        run = super().run(release, config, met, window, workdir, timeout)
        return ModelRun(run.result.drop(columns="lon"))


@pytest.mark.parametrize(
    ("model", "release", "error", "message"),
    [
        (_Forward, RELEASES[0], AssertionError, "ages must be <= 0"),
        (_TooLong, RELEASES[0], AssertionError, "within n_hours"),
        (_NoReleaseRows, RELEASES[1], SimulationError, "no row at age = 0"),
        (_NoLon, RELEASES[0], AssertionError, "lon"),
    ],
)
def test_a_run_that_breaks_a_promise_fails(model, release, error, message):
    with pytest.raises(error, match=message):
        check_model(model(), ToyConfig(n_hours=-3), ToyMet(), release)


def test_a_run_needs_a_met_of_the_models_own():
    with pytest.raises(AssertionError, match="met must be a ToyMet"):
        check_model(ToyModel(), ToyConfig(n_hours=-3), {"weather": "x"}, RELEASES[0])


# ---------------------------------------------------------------------------
# Each promise, broken by a toy that breaks only it
# ---------------------------------------------------------------------------


class _NoTimeout(ToyModel):
    """A run that does not take the timeout the core passes."""

    def run(self, release, config, met, window, workdir=None):
        return super().run(release, config, met, window, workdir)


class _ModelField(ToyConfig):
    model: str = "fast"


class _NamedModel(ToyModel):
    config_class = _ModelField


class _NoName(ToyModel):
    name = ""


class _NotTransport(ToyModel):
    config_class = ToyMet


class _PlainMet(ToyModel):
    met_config_class = dict


class _IntSha(ToyModel):
    def sha256(self, config):
        return 123


class _IntKeys(ToyModel):
    def data_files(self, config):
        return {1: "abc"}


class _Frame(ToyModel):
    def run(self, release, config, met, window, workdir=None, timeout=None):
        return super().run(release, config, met, window, workdir, timeout).result


class _ByteLog(ToyModel):
    def run(self, release, config, met, window, workdir=None, timeout=None):
        run = super().run(release, config, met, window, workdir, timeout)
        return ModelRun(run.result, log=b"done")


class _HalfMinutes(ToyModel):
    def run(self, release, config, met, window, workdir=None, timeout=None):
        run = super().run(release, config, met, window, workdir, timeout)
        return ModelRun(run.result.assign(age=run.result["age"] + 0.5))


class _LosesAParticle(ToyModel):
    def release_rows(self, particles, release, config):
        return super().release_rows(particles, release, config).iloc[1:]


class _ShuffledCheck(ToyModel):
    def check_releases(self, releases, config):
        return pd.DataFrame(columns=["release", "message", "check"])


class _TwoRealizations(ToyConfig):
    def realizations(self, n):
        return super().realizations(2)


class _FewRealizations(ToyModel):
    config_class = _TwoRealizations


@pytest.mark.parametrize(
    ("model", "release", "error", "message"),
    [
        (_NoTimeout, RELEASES[0], AssertionError, "timeout"),
        (_NamedModel, None, AssertionError, r"field named \['model'\]"),
        (_NoName, None, AssertionError, "name must be a string"),
        (_NotTransport, None, AssertionError, "TransportConfig"),
        (_PlainMet, None, AssertionError, "must subclass runtraj.models.MetConfig"),
        (_IntSha, None, AssertionError, "ModelInfo"),
        (_IntKeys, None, AssertionError, "ModelInfo"),
        (_Frame, RELEASES[0], AssertionError, "must return a ModelRun"),
        (_ByteLog, RELEASES[0], AssertionError, "log must be a string"),
        (_HalfMinutes, RELEASES[0], AssertionError, "cannot be stored"),
        (_LosesAParticle, RELEASES[1], SimulationError, "no row for"),
        (_ShuffledCheck, RELEASES[0], AssertionError, "check, release"),
        (_FewRealizations, None, AssertionError, "3 configs"),
    ],
    ids=lambda v: v.__name__.strip("_") if isinstance(v, type) else "",
)
def test_each_broken_promise_fails(model, release, error, message):
    config_class = model.config_class
    if not issubclass(config_class, TransportConfig):
        config_class = ToyConfig
    config = config_class(n_hours=-3, seed=1)
    with pytest.raises(error, match=message):
        check_model(model(), config, ToyMet(), release)


class _RefusesColumns(ToyModel):
    def check_releases(self, releases, config):
        columns = releases[releases["kind"] == "column"]
        return pd.DataFrame(
            {
                "check": "release_refused",
                "release": columns["release"].unique(),
                "message": "The toy runs no column.",
            },
            columns=["check", "release", "message"],
        )


def test_a_release_the_model_refuses_is_refused_and_not_run():
    check_model(_RefusesColumns(), ToyConfig(n_hours=-3), ToyMet(), RELEASES[1])
    check_model(_RefusesColumns(), ToyConfig(n_hours=-3), ToyMet(), RELEASES[0])


class _PathSetting(ToyConfig):
    """Records a path, which a settings file stores as a string."""

    def settings(self) -> dict[str, Any]:
        return {**super().settings(), "table": Path("/data/table.csv")}


class _PathModel(ToyModel):
    config_class = _PathSetting


def test_a_record_the_core_stores_passes():
    check_model(_PathModel(), _PathSetting(n_hours=-3))

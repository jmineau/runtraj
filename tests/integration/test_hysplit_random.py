"""
HYSPLIT's random numbers and ``SEED``, pinned for each HYSPLIT version.

runtraj relies on a seed giving the same run each time and another seed a
different one (``seed``, ``realizations``), and on ``krand: 4`` differing from
run to run. A test marked ``xfail`` on a version pins a known HYSPLIT bug
there: when HYSPLIT fixes it, the test passes, which ``strict`` makes a
failure, so runtraj's handling of it is looked at again.
"""

from __future__ import annotations

import pytest

import runtraj
from runtraj.models import run_model
from runtraj.models.hysplit import HysplitConfig
from runtraj.releases import PointRelease

from ..conftest import integration
from .conftest import REFERENCE_TIME, reference_met, run_raw, since

pytestmark = [integration, pytest.mark.usefixtures("hysplit_build")]

RELEASE = PointRelease(
    time=REFERENCE_TIME, longitude=-112.0, latitude=40.5, altitude=5.0
)

#: A short particle run; ``seed`` 42 is written as ``SEED=-43``.
PARTICLES = {"n_hours": -1, "numpar": 100, "krand": 2}

#: A short concentration run with ``krand: 4``, under HYSPLIT's default ``IDSP=1``.
CONCENTRATION = HysplitConfig(
    n_hours=1,
    numpar=100,
    krand=4,
    mode="concentration",
    pollutants=[{"name": "TRCA"}],
    grids=[{"name": "local", "levels": [0, 100]}],
)


def _seeded(tmp_path, met_dir, name, seed=42, edit=None):
    config = HysplitConfig(**PARTICLES, seed=seed)
    return run_raw(tmp_path / name, RELEASE, config, met_dir, edit)


def _concentration(tmp_path, met_dir, name, config=CONCENTRATION):
    run = run_model("hysplit", RELEASE, config, reference_met(met_dir), tmp_path / name)
    return run.result["local"].to_dataset()["TRCA"].values


def test_a_seed_repeats_its_run_and_another_seed_differs(tmp_path, met_dir):
    first = _seeded(tmp_path, met_dir, "first")
    assert first.equals(_seeded(tmp_path, met_dir, "again"))
    assert not first.equals(_seeded(tmp_path, met_dir, "other", seed=43))


def test_a_positive_seed_gives_the_run_of_its_negative(
    tmp_path, met_dir, hysplit_build, request
):
    # HYSPLIT 5.4.2 and earlier ignore a positive SEED under KRAND=2 and give
    # the unseeded run, which is why runtraj writes -(seed + 1); the
    # development branch reads it.
    request.applymarker(
        pytest.mark.xfail(
            not since(hysplit_build, (5, 5, 0)),
            reason="HYSPLIT 5.4.2 and earlier ignore a positive SEED",
            strict=True,
        )
    )
    negative = _seeded(tmp_path, met_dir, "negative")
    positive = _seeded(
        tmp_path, met_dir, "positive", edit=lambda t: t.replace("SEED=-43,", "SEED=43,")
    )
    assert positive.equals(negative)


def test_krand_4_particles_differ_each_run(tmp_path, met_dir):
    def run(name):
        return runtraj.run_trajectories(
            RELEASE, reference_met(met_dir), model="hysplit",
            workdir=tmp_path / name, n_hours=-1, numpar=100, krand=4,
        )  # fmt: skip

    assert not run("first").equals(run("second"))


def test_krand_4_concentration_differs_each_run(
    tmp_path, met_dir, hysplit_build, request
):
    # The development branch reseeds KRAND=4 from SEED when IDSP is not 2
    # (the concentration mode's default), after the clock seed was drawn.
    request.applymarker(
        pytest.mark.xfail(
            hysplit_build == "dev",
            reason="the development branch makes KRAND=4 repeat when IDSP is not 2",
            strict=True,
        )
    )
    first = _concentration(tmp_path, met_dir, "first")
    assert (first != _concentration(tmp_path, met_dir, "second")).any()


def test_krand_4_realizations_differ(tmp_path, met_dir):
    """Each realization of a ``krand: 4`` variant gets its own seed, so they differ on every version."""
    zero, one = CONCENTRATION.realizations(2)
    first = _concentration(tmp_path, met_dir, "zero", zero)
    assert (first != _concentration(tmp_path, met_dir, "one", one)).any()


def test_omp_num_threads_does_not_change_the_particles(tmp_path, met_dir, monkeypatch):
    # runtraj's Slurm and PBS scripts export OMP_NUM_THREADS. The development
    # branch's hycs_std runs threads only when given -tN, and each thread
    # count gives other particles, so a run must not read OMP_NUM_THREADS.
    monkeypatch.setenv("OMP_NUM_THREADS", "1")
    one = _seeded(tmp_path, met_dir, "one")
    monkeypatch.setenv("OMP_NUM_THREADS", "4")
    assert _seeded(tmp_path, met_dir, "four").equals(one)

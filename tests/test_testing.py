"""runtraj.testing: a stage run end to end through a project, on the replay transport model."""

import datetime as dt

import pytest

from runtraj import PointRelease
from runtraj.models import get_model
from runtraj.testing import (
    REPLAY_MODEL,
    ReplayConfig,
    ReplayMet,
    check_model,
    run_stage,
)

from .fixtures.output import fake_particles
from .fixtures.stages import COUNT, DOUBLE

RELEASE = PointRelease(
    time=dt.datetime(2023, 1, 1, 12), longitude=-111.85, latitude=40.77, altitude=5.0
)


def test_a_stage_runs_end_to_end_and_its_file_is_returned(tmp_path):
    particles = fake_particles(RELEASE)

    sim = run_stage(COUNT, particles, RELEASE, tmp_path, settings={"scale": 2})

    assert sim.path("count").read_text() == str(2 * len(particles))


def test_a_stage_that_reads_another_runs_after_it(tmp_path):
    particles = fake_particles(RELEASE)

    sim = run_stage(
        DOUBLE, particles, RELEASE, tmp_path, others={"count": {"class": COUNT}}
    )

    assert sim.path("double").read_text() == str(2 * len(particles))


@pytest.mark.parametrize(
    ("settings", "says"),
    [
        ({"fail": True}, "the count was told to fail"),
        ({"silent": True}, "did not complete"),
    ],
)
def test_a_stage_that_breaks_a_project_s_promise_fails(tmp_path, settings, says):
    with pytest.raises(AssertionError, match=says):
        run_stage(COUNT, fake_particles(RELEASE), RELEASE, tmp_path, settings=settings)


def test_the_replay_model_keeps_runtraj_s_promises(tmp_path):
    particles = fake_particles(RELEASE)
    particles.to_parquet(tmp_path / f"{RELEASE.id}.parquet")

    check_model(
        get_model(REPLAY_MODEL),
        ReplayConfig(n_hours=-1),
        ReplayMet(directory=tmp_path),
        RELEASE,
    )

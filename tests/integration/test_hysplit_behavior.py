"""
HYSPLIT behaviors runtraj works around or reports, pinned for each HYSPLIT version.

A test marked ``xfail`` on a version pins a known HYSPLIT bug there: when
HYSPLIT fixes it, the test passes, which ``strict`` makes a failure, so
runtraj's handling of it is looked at again.
"""

from __future__ import annotations

import datetime as dt
import os
import re

import numpy as np
import pytest

from runtraj.models.hysplit import HysplitConfig
from runtraj.models.hysplit.driver import hysplit_version
from runtraj.releases import MultiPointRelease, PointRelease

from ..conftest import integration
from .conftest import BUILD_VARIABLE, REFERENCE_TIME, run_raw, since

pytestmark = [integration, pytest.mark.usefixtures("hysplit_build")]

COLUMNS = ["time", "indx", "long", "lati", "zagl"]

#: Midday in Salt Lake City, when the shortwave flux is well above 0.
MIDDAY = PointRelease(
    time=dt.datetime(2021, 1, 14, 20), longitude=-112.0, latitude=40.5, altitude=5.0
)


def test_hycs_std_prints_its_own_version(hysplit_build, request):
    # runtraj records the printed version with each run and gates settings on it.
    if BUILD_VARIABLE not in os.environ:
        pytest.skip(f"{BUILD_VARIABLE} does not name the build")
    request.applymarker(
        pytest.mark.xfail(
            hysplit_build == "dev",
            reason="the development branch prints hysplit.v5.4.2",
            strict=True,
        )
    )
    printed = hysplit_version()
    if hysplit_build == "dev":
        assert printed != "hysplit.v5.4.2"
    else:
        assert printed == f"hysplit.v{hysplit_build}"


def _dswf(tmp_path, met_dir, name, edit=None):
    config = HysplitConfig(n_hours=-1, numpar=20, varsiwant=[*COLUMNS, "dswf"])
    return run_raw(tmp_path / name, MIDDAY, config, met_dir, edit)["dswf"]


def test_the_dswf_runtraj_writes_is_filled(tmp_path, met_dir, hysplit_build, request):
    # Before 5.2.0 hycs_std computes the shortwave flux only for resistance
    # deposition, so the column is 0 in a particle run.
    request.applymarker(
        pytest.mark.xfail(
            not since(hysplit_build, (5, 2, 0)),
            reason="HYSPLIT before 5.2.0 writes dswf as 0 without deposition",
            strict=True,
        )
    )
    assert (_dswf(tmp_path, met_dir, "upper") > 0).any()


def test_a_lower_case_dswf_gives_the_column_of_upper_case(
    tmp_path, met_dir, hysplit_build, request
):
    # From 5.2.0 hycs_std turns on the flux by a case-sensitive comparison
    # with 'DSWF', so runtraj writes it in upper case.
    request.applymarker(
        pytest.mark.xfail(
            since(hysplit_build, (5, 2, 0)),
            reason="HYSPLIT 5.2.0 and later fill dswf only for 'DSWF'",
            strict=True,
        )
    )
    upper = _dswf(tmp_path, met_dir, "upper")
    lower = _dswf(tmp_path, met_dir, "lower", lambda t: t.replace("'DSWF'", "'dswf'"))
    assert lower.equals(upper)


def _mixed_layer(tmp_path, met_dir, name, edit=None, **settings):
    config = HysplitConfig(
        n_hours=-1, numpar=20, krand=2, seed=42, kmixd=0,
        varsiwant=[*COLUMNS, "mlht"], **settings,
    )  # fmt: skip
    return run_raw(tmp_path / name, MIDDAY, config, met_dir, edit)


def test_ziscale_scales_the_mixed_layer(tmp_path, met_dir):
    # From 5.3.0 hycs_std adds 2 to WINDERRTF once it reads ZICONTROL;
    # runtraj writes 2 less there, so ziscale alone scales the mixed layer.
    plain = _mixed_layer(tmp_path, met_dir, "plain")
    scaled = _mixed_layer(tmp_path, met_dir, "scaled", ziscale=0.5)
    minute = ["particle", "mlht"]
    both = plain.loc[plain["age"] == -1, minute].merge(
        scaled.loc[scaled["age"] == -1, minute], on="particle"
    )
    ratio = (both["mlht_y"] / both["mlht_x"]).to_numpy()
    print(f"\nmlht scaled / plain on the first rows: {np.unique(ratio.round(3))}")
    np.testing.assert_allclose(ratio, 0.5, atol=0.05)


def test_zicontrol_leaves_winderrtf_alone(tmp_path, met_dir, hysplit_build, request):
    # Before 5.3.0 a run with ZICONTROL and WINDERRTF=0 gets no errors; from
    # 5.3.0 hycs_std makes it 2, wants a ZIERR file, and without one stops.
    request.applymarker(
        pytest.mark.xfail(
            since(hysplit_build, (5, 3, 0)),
            reason="HYSPLIT 5.3.0 and later add 2 to WINDERRTF with ZICONTROLTF=1",
            strict=True,
        )
    )
    particles = _mixed_layer(
        tmp_path, met_dir, "zicontrol",
        lambda t: re.sub(r"WINDERRTF=-?\d+,", "WINDERRTF=0,", t), ziscale=0.5,
    )  # fmt: skip
    assert not particles.empty


def test_the_wind_falls_to_zero_below_the_first_level(
    tmp_path, met_dir, hysplit_build, request
):
    # Up to 5.2.3 a particle below the first internal level (10 m here) gets
    # the level-1 wind scaled down to 0 at the ground; from 5.3.0 it gets the
    # full level-1 wind.
    request.applymarker(
        pytest.mark.xfail(
            since(hysplit_build, (5, 3, 0)),
            reason="HYSPLIT 5.3.0 and later give the full level-1 wind below it",
            strict=True,
        )
    )
    release = MultiPointRelease(
        time=REFERENCE_TIME,
        longitudes=[-112.0, -111.99],
        latitudes=[40.5, 40.5],
        altitudes=[1.0, 8.0],
    )
    config = HysplitConfig(n_hours=-1, numpar=1000, krand=2, seed=42, varsiwant=COLUMNS)
    particles = run_raw(tmp_path, release, config, met_dir)

    # Each point's mean move in the first minute; its first 500 particles are the 1 m point's.
    rows = particles.loc[particles["age"] == -1]
    low = rows["particle"] <= 500
    move = {}
    for name, rows_at, lon in (
        ("1 m", rows[low], -112.0),
        ("8 m", rows[~low], -111.99),
    ):
        dx = (rows_at["lon"].mean() - lon) * np.cos(np.radians(40.5))
        dy = rows_at["lat"].mean() - 40.5
        move[name] = float(np.hypot(dx, dy) * 111_195)
    print(f"\nmean move in the first minute, m: {move}")
    assert move["1 m"] / move["8 m"] < 0.55

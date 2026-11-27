"""
Met sampled at particle positions with arlmet, against the columns HYSPLIT wrote.

HYSPLIT writes ``mlht``, ``dens``, ``pres`` and ``zsfc`` from its own copy of
the met. :func:`runtraj.sampling.sample_met` reads the same met files with
arlmet at each particle's position and time. Comparing the two checks
arlmet's interpolation against an independent reference. Run with ``-s`` to
see the spread of each column. The bounds hold the spread measured on these
two runs with a margin, so a wrong variable, unit, or time fails and the
known differences pass:

``zsfc``
    Both interpolate the terrain bilinearly. HYSPLIT does it where the
    particle was before its turbulent step and writes ``lon`` and ``lat`` to
    four decimals, so a few meters differ on steep terrain (bias under
    0.2 m, 95 % within 3 m).
``mlht``
    With ``kmixd: 0`` HYSPLIT uses the met's ``PBLH``, at least ``kmix0``,
    at the grid point nearest the particle, averaged over the start and end
    of its step. ``sample_met`` interpolates it bilinearly (median
    difference 16 m in winter, 30 m in summer).
``pres``
    arlmet reads HYSPLIT's own internal levels, for the run's top. What is left
    is mostly where HYSPLIT took the value, as for ``zsfc`` (median difference
    0.02 hPa, 95 % within 0.3 hPa).
``dens``
    The sampled density is 0.17 to 0.57 % higher, more in moister air, which is
    not traced.

HYSPLIT's default, ``kmixd: 3``, finds the mixed layer from the met's
profile, and ``kmixd: 1`` from its temperature alone;
:func:`runtraj.sampling.calc_pbl_height` finds it each way, and as the met's
``PBLH`` (``kmixd: 0``).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from runtraj.models import run_model
from runtraj.models.hysplit import HysplitConfig
from runtraj.releases import PointRelease
from runtraj.sampling import calc_pbl_height, sample_met

from ..conftest import integration
from .conftest import REFERENCE_TIME, reference_met

#: A summer time the test met cache covers, with six hours before it.
REFERENCE_SUMMER_TIME = dt.datetime(2021, 7, 15, 6, 0)

COLUMNS = ["mlht", "dens", "pres", "zsfc"]
KMIX0 = 150


def _run(
    met_dir: Path,
    workdir: Path,
    time: dt.datetime,
    z_top: float = 25000.0,
) -> tuple[pd.DataFrame, list]:
    """Run HYSPLIT from the WBB tower for 6 hours and return its particles and met files."""
    release = PointRelease(
        time=time, longitude=-111.8479, latitude=40.7660, altitude=5.0
    )
    config = HysplitConfig(
        n_hours=-6,
        numpar=200,
        kmixd=0,  # the met's PBLH, so HYSPLIT's mlht can be compared
        kmix0=KMIX0,
        z_top=z_top,
        varsiwant=["time", "indx", "long", "lati", "zagl", "foot", *COLUMNS],
    )
    run = run_model("hysplit", release, config, reference_met(met_dir), workdir, 900)
    particles = run.result.copy()
    particles["time"] = pd.Timestamp(time) + pd.to_timedelta(
        particles["age"], unit="min"
    )
    return particles, run.met_files


def _spread(hysplit: pd.Series, sampled: pd.Series) -> dict[str, float]:
    """Return the statistics of ``sampled - hysplit``."""
    d = (sampled - hysplit).to_numpy(dtype=float)
    ok = np.isfinite(d)
    a = np.abs(d[ok])
    return {
        "n": int(ok.sum()),
        "nan": int((~ok).sum()),
        "bias": float(d[ok].mean()),
        "sd": float(d[ok].std()),
        "median_abs": float(np.median(a)),
        "p95_abs": float(np.percentile(a, 95)),
        "max_abs": float(a.max()),
        "rel_bias": float((d[ok] / hysplit.to_numpy(dtype=float)[ok]).mean()),
    }


@integration
@pytest.mark.parametrize(
    "time", [REFERENCE_TIME, REFERENCE_SUMMER_TIME], ids=["winter", "summer"]
)
def test_sampled_met_matches_hysplit_columns(met_dir, tmp_path, time):
    hysplit, met_files = _run(met_dir, tmp_path / "run", time)

    sampled = sample_met(hysplit.drop(columns=COLUMNS), met_files)
    sampled["mlht"] = np.maximum(sampled["mlht"], KMIX0)

    spread = pd.DataFrame({c: _spread(hysplit[c], sampled[c]) for c in COLUMNS}).T
    print(f"\nsample_met minus HYSPLIT, {time:%Y-%m-%d %H:%M}, {len(hysplit)} rows:")
    print(spread.to_string(float_format=lambda v: f"{v:.4g}"))
    zagl = hysplit["zagl"]
    for lo, hi in [(0, 100), (100, 500), (500, 1000), (1000, 2000), (2000, 1e5)]:
        rows = (zagl >= lo) & (zagl < hi)
        if rows.any():
            d = (sampled["pres"] - hysplit["pres"])[rows]
            print(
                f"pres, zagl {lo:g} to {hi:g} m: {int(rows.sum())} rows, "
                f"bias {d.mean():+.3g} hPa, sd {d.std():.3g}"
            )

    assert not sampled[COLUMNS].isna().any(axis=None)
    assert abs(spread.loc["zsfc", "bias"]) < 1.0
    assert spread.loc["zsfc", "p95_abs"] < 6.0
    assert abs(spread.loc["mlht", "bias"]) < 30.0
    assert spread.loc["mlht", "median_abs"] < 50.0
    assert abs(spread.loc["pres", "bias"]) < 0.1
    assert spread.loc["pres", "p95_abs"] < 1.0
    assert abs(spread.loc["dens", "rel_bias"]) < 0.015


@integration
def test_a_run_with_another_top_is_sampled_with_that_top(met_dir, tmp_path):
    """HYSPLIT's internal levels depend on its top, so the run's top reads what it read."""
    z_top = 10000.0
    hysplit, met_files = _run(met_dir, tmp_path / "run", REFERENCE_TIME, z_top)
    particles = hysplit.drop(columns=["pres", "dens"])

    same = sample_met(particles, met_files, ["pres", "dens"], z_top=z_top)
    default = sample_met(particles, met_files, ["pres", "dens"])
    own_levels = sample_met(particles, met_files, ["pres", "dens"], z_top=None)
    message = tmp_path / "run" / "MESSAGE"
    printed = sample_met(
        particles, met_files, ["pres", "dens"], z_top=z_top, message=message
    )

    for name, sampled in (("z_top", same), ("default", default), ("None", own_levels)):
        s = _spread(hysplit["pres"], sampled["pres"])
        print(f"\npres minus HYSPLIT with a 10 km top, {name}: {s}")
    spread = _spread(hysplit["pres"], same["pres"])
    assert spread["nan"] == 0
    # The grid the run printed is the one arlmet picks for its top.
    pd.testing.assert_frame_equal(printed, same)
    assert (
        spread["median_abs"] < _spread(hysplit["pres"], default["pres"])["median_abs"]
    )


@integration
@pytest.mark.parametrize(
    ("method", "kmixd"),
    [("hysplit", 3), ("hysplit_temperature", 1), ("met", 0)],
)
@pytest.mark.parametrize(
    "time",
    [dt.datetime(2021, 1, 14, 22), REFERENCE_TIME, REFERENCE_SUMMER_TIME],
    ids=["winter-day", "winter-night", "summer"],
)
def test_calc_pbl_height_finds_the_mixed_layer_hysplit_finds(
    met_dir, tmp_path, time, method, kmixd
):
    """HYSPLIT's ``mlht`` is the height its ``kmixd`` finds, at least ``kmix0``."""
    release = PointRelease(time=time, longitude=-112.0, latitude=40.5, altitude=5.0)
    config = HysplitConfig(
        n_hours=-6,
        numpar=200,
        krand=2,
        seed=42,
        kmixd=kmixd,
        kmix0=KMIX0,
        varsiwant=["time", "indx", "long", "lati", "zagl", "mlht"],
    )
    run = run_model(
        "hysplit", release, config, reference_met(met_dir), tmp_path / "run", 900
    )
    hysplit = run.result.assign(
        time=pd.Timestamp(time) + pd.to_timedelta(run.result["age"], unit="min")
    )

    ours = np.maximum(calc_pbl_height(hysplit, run.met_files, method=method), KMIX0)

    ratio = ours / hysplit["mlht"].to_numpy()
    close = np.mean(np.abs(ratio - 1) < 0.02)
    print(
        f"\ncalc_pbl_height ({method}) / HYSPLIT's mlht: median {np.median(ratio):.4f}, "
        f"5-95 % {np.quantile(ratio, 0.05):.3f} to {np.quantile(ratio, 0.95):.3f}, "
        f"rows within 2 % {close:.3f}"
    )
    assert np.isfinite(ours).all()
    assert np.median(ratio) == pytest.approx(1.0, abs=0.01)
    assert close > 0.6

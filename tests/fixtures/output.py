"""
Particles and variants for tests of the output directory.

The particles are small and random but fixed by a seed, for a release at
the WBB site.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

from runtraj.config import Variant
from runtraj.output import Output
from runtraj.releases import PointRelease

from .factories import MET, make_release, make_variant


def output_variant(name: str = "hrrr", **overrides) -> Variant:
    """Return a resolved variant on :data:`MET`; *overrides* change the transport fields."""
    return make_variant(name, met_config=MET, **overrides)


VARIANT = output_variant()


def release_at(hour: int = 12, day: int = 15) -> PointRelease:
    """Return the WBB release at *hour* on 2024-07-*day*."""
    return make_release(dt.datetime(2024, 7, day, hour))


def fake_particles(release: PointRelease, n: int = 50) -> pd.DataFrame:
    """Return *n* particles of ten one-minute steps, random but fixed by the release's time."""
    rng = np.random.default_rng(int(release.time.timestamp()) % 1000)
    steps = np.arange(-60, -601, -60, dtype=float)  # seconds
    particle = np.repeat(np.arange(1, n + 1, dtype=float), len(steps))
    time = np.tile(steps, n)
    data = pd.DataFrame(
        {
            "age": time,
            "particle": particle,
            "lon": -111.85 + rng.normal(0, 0.1, len(time)),
            "lat": 40.77 + rng.normal(0, 0.1, len(time)),
            "zagl": rng.uniform(0, 500, len(time)),
            "foot": rng.uniform(0, 0.1, len(time)),
        }
    )
    data["time"] = pd.Timestamp(release.time) + pd.to_timedelta(data["age"], unit="s")
    return data


def write_one(out: Output, variant: Variant = VARIANT, release=None) -> Path:
    """Write one release's particles for *variant*, making its folder."""
    release = release if release is not None else release_at()
    return out.write_particles(variant, release, fake_particles(release), [])


def as_written_before_the_rename(path: Path) -> Path:
    """Rewrite a particle file as it was written when a release was a receptor."""
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    names = ["receptor" if n == "release" else n for n in table.column_names]
    meta = {
        (b"runtraj:receptor" if k == b"runtraj:release" else k): v
        for k, v in (table.schema.metadata or {}).items()
    }
    pq.write_table(table.rename_columns(names).replace_schema_metadata(meta), path)
    return path

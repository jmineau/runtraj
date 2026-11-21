"""
A toy transport model, made unlike HYSPLIT, for the tests of the core.

runtraj's core must work with any transport model the
:class:`runtraj.models.TransportModel` protocol describes. A second real
model does not show that it does, since it is written to fit the core; this
one differs from HYSPLIT wherever it can:

- it runs no program, so it has no executable, needs no workdir, and
  writes no log;
- it reads no met files, so it has no ``check_met`` or ``fetch_met``;
- its first row is one output step after the release: it writes no row
  at ``age = 0``, and says where each particle started through
  ``release_rows`` instead;
- it numbers its particles 10, 20, 30, ... (:data:`PARTICLE_STEP`), not 1 to N;
- its settings have names of their own (``n_particles``, ``step``) and
  share none of HYSPLIT's;
- it writes no ``foot``;
- with ``counts: true`` it makes a concentration result
  (:mod:`runtraj.concentration`) instead of particles: the count of its
  particles on one small grid, its own way, as a model of another package
  would.

A config names it by import path, ``class: tests.fixtures.models.ToyModel``
(:data:`TOY`), as it names a model in another package. Like such a model,
it imports only runtraj's public names.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pandas as pd
import xarray as xr
from pydantic import BaseModel, ConfigDict, Field

from runtraj import Release
from runtraj.models import ModelRun, TransportConfig

#: The toy model's import path, as a ``models`` block names it.
TOY = "tests.fixtures.models.ToyModel"

#: The toy numbers its particles ``PARTICLE_STEP``, ``2 * PARTICLE_STEP``, ...
PARTICLE_STEP = 10

#: How far a particle moves west each hour, in degrees.
DRIFT = 0.05

#: How far a particle rises each hour, in meters.
RISE = 10.0


class ToyConfig(TransportConfig):
    """The toy model's settings: none of them HYSPLIT's."""

    UNRECORDED: ClassVar[frozenset[str]] = frozenset({"label"})

    n_particles: int = Field(4, gt=0, description="Particles released.")
    step: int = Field(60, gt=0, description="Minutes between a particle's rows.")
    label: str | None = Field(
        None,
        description="A note kept out of the run's settings, as a build's folder is.",
    )
    counts: bool = Field(
        False,
        description="Make a concentration result, the particles counted on a grid, in place of particles.",
    )

    def settings(self) -> dict[str, Any]:
        """Return the run's record: a particles run's names no ``counts``, as HYSPLIT's names no ``mode``."""
        record = super().settings()
        if not self.counts:
            del record["counts"]
        return record


class ToyMet(BaseModel):
    """
    The toy's met: a name and no files.

    It accepts other keys, so a met that HYSPLIT reads in the same project
    (its ``directory``) is a valid toy met too.
    """

    model_config = ConfigDict(extra="allow")

    weather: str = Field("toy", description="Which weather the toy pretends to read.")

    def settings(self) -> dict[str, Any]:
        """Return what a run records of the met."""
        return {"weather": self.weather}


class ToyModel:
    """
    Releases particles at the release and drifts them west and up, one row per step.

    With a seed the run repeats; realization ``k`` (``seed + k``) differs from
    the others.
    """

    name = "toy"
    config_class = ToyConfig
    met_config_class = ToyMet
    needs_workdir = False

    def result(self, config: ToyConfig) -> str:
        """The toy makes particles, or with ``counts`` a concentration result."""
        return "concentration" if config.counts else "particles"

    def sha256(self, config: ToyConfig) -> None:
        """The toy runs no executable."""
        return None

    def version(self, config: ToyConfig) -> str:
        """Return the toy's version."""
        return "toy-1"

    def data_files(self, config: ToyConfig) -> None:
        """The toy reads no data files."""
        return None

    def run(
        self,
        release: Release,
        config: ToyConfig,
        met: ToyMet,
        window: tuple[dt.datetime, dt.datetime],
        workdir: Path | None = None,
        timeout: int | None = None,
    ) -> ModelRun:
        """Return the particles of one release: no log and no met files."""
        rng = np.random.default_rng(config.seed)
        n = config.n_particles
        lon, lat, alt = _starts(release, n, rng)
        sign = -1 if config.n_hours < 0 else 1
        ages = np.arange(config.step, abs(config.n_hours) * 60 + 1, config.step)
        hours = np.repeat(ages, n) / 60.0
        jitter = rng.normal(0.0, 0.001, size=hours.size)
        particles = pd.DataFrame(
            {
                "particle": np.tile(np.arange(1, n + 1) * PARTICLE_STEP, len(ages)),
                "age": np.repeat(sign * ages, n),
                "lon": np.tile(lon, len(ages)) - DRIFT * hours + jitter,
                "lat": np.tile(lat, len(ages)) + jitter,
                "zagl": np.tile(alt, len(ages)) + RISE * hours,
            }
        )
        if config.counts:
            return ModelRun(result=_counted(particles, release))
        return ModelRun(result=particles)

    def release_rows(self, particles: pd.DataFrame, release: Release) -> pd.DataFrame:
        """Return where each particle started: its first row, with the drift taken back."""
        first = particles.loc[
            particles["age"].abs().groupby(particles["particle"]).idxmin()
        ]
        hours = first["age"].abs().to_numpy() / 60.0
        return pd.DataFrame(
            {
                "particle": first["particle"].to_numpy(),
                "lon": first["lon"].to_numpy() + DRIFT * hours,
                "lat": first["lat"].to_numpy(),
                "height": first["zagl"].to_numpy() - RISE * hours,
            }
        )


#: The toy's grid: 3 by 3 cells of this size, in degrees, centered on the release.
CELL = 0.1


def _counted(particles: pd.DataFrame, release: Release) -> xr.DataTree:
    """Return the particles of the last step counted on a 3 by 3 grid at one level, in one sample."""
    lon0, lat0, _ = release.coords()[0]
    last = particles[particles["age"].abs() == particles["age"].abs().max()]
    lats = lat0 + CELL * np.arange(-1, 2)
    lons = lon0 + CELL * np.arange(-1, 2)
    i = np.clip(np.round((last["lat"] - lat0) / CELL).astype(int) + 1, 0, 2)
    j = np.clip(np.round((last["lon"] - lon0) / CELL).astype(int) + 1, 0, 2)
    counts = np.zeros((1, 1, 3, 3), dtype=np.float32)
    np.add.at(counts, (0, 0, i.to_numpy(), j.to_numpy()), 1.0)
    time = pd.Timestamp(release.time)
    grid = xr.Dataset(
        {"TOY": (("time", "z", "lat", "lon"), counts)},
        coords={
            "time": [time],
            "time_end": ("time", [time]),
            "z": [100],
            "lat": lats,
            "lon": lons,
        },
    )
    return xr.DataTree.from_dict({"cells": grid})


def _starts(
    release: Release, n: int, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Return where each of *n* particles starts.

    A column release's particles start at random heights between its bottom
    and top; the particles of a release of points are dealt out over the
    points in turn.
    """
    coords = np.array(release.coords(), dtype=float)
    if release.kind == "column":
        bottom, top = coords[0, 2], coords[-1, 2]
        alt = rng.uniform(bottom, top, size=n)
        return np.full(n, coords[0, 0]), np.full(n, coords[0, 1]), alt
    picked = coords[np.arange(n) % len(coords)]
    return picked[:, 0], picked[:, 1], picked[:, 2]

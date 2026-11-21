"""
Two toy stages, for the tests of the stage interface.

They stand for a stage in another package, which the code that runs stages
never imports by name. ``count`` reads the particles and ``double`` reads ``count``'s file, so
the two are the shortest chain there is. A config names them by import path:
``class: tests.fixtures.stages.CountStage``. Like a stage in another package,
they import only runtraj's public names (``tests/test_stages.py`` checks it).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from pydantic import BaseModel, ConfigDict

from runtraj import Simulation

if TYPE_CHECKING:
    from upath import UPath

COUNT = "tests.fixtures.stages.CountStage"
DOUBLE = "tests.fixtures.stages.DoubleStage"

ECHO = "tests.fixtures.stages.EchoStage"
CELLS = "tests.fixtures.stages.CellsStage"

#: Each call of a toy stage's ``run``, as (stage, release id, variant), in order.
RUNS: list[tuple[str, str, str]] = []

#: The name of the path each call of a toy stage's ``run`` was handed.
PATHS: list[str] = []


class CountConfig(BaseModel):
    """Settings of :class:`CountStage`."""

    model_config = ConfigDict(extra="forbid")

    scale: int = 1
    fail: bool = False
    silent: bool = False
    stale: bool = False


class CountStage:
    """Writes the number of particle rows of a simulation, times ``scale``, as text."""

    name = "count"
    config_class = CountConfig
    suffix = ".txt"

    def resolve(
        self,
        config: CountConfig,
        model: str,
        model_settings: Mapping[str, Any],
        directory: Path | None,
    ) -> CountConfig:
        """Refuse a transport model that releases 13 particles."""
        if model_settings.get("numpar") == 13:
            raise ValueError(
                f"it cannot count the particles of {model} with numpar 13."
            )
        return config

    def settings(self, config: CountConfig) -> dict[str, Any]:
        """Return the record of a count's settings."""
        return config.model_dump(mode="json")

    def check(
        self, config: CountConfig, releases: pd.DataFrame, directory: Path | None
    ) -> list[tuple[str, str | None, str]]:
        """List the releases south of the equator, which this stage pretends it cannot count."""
        south = releases[releases["latitude"] < 0].drop_duplicates("release")
        return [
            ("count", str(rid), "is south of the equator") for rid in south["release"]
        ]

    def run(self, config: CountConfig, sim: Simulation, path: Path | UPath) -> None:
        """
        Write the count of *sim*'s particle rows to *path*.

        With ``fail`` it writes half a file and raises; with ``silent`` it
        returns without writing one; with ``stale`` it raises an error a
        retry may get past.
        """
        RUNS.append((self.name, sim.release.id, sim.variant.name))
        PATHS.append(path.name)
        if config.silent:
            return
        if config.stale:
            raise ConnectionError("the count lost its connection")
        if config.fail:
            path.write_text("half a")
            raise RuntimeError("the count was told to fail")
        path.write_text(str(len(sim.particles) * config.scale))


class DoubleConfig(BaseModel):
    """Settings of :class:`DoubleStage`."""

    model_config = ConfigDict(extra="forbid")

    add: int = 0


class DoubleStage:
    """Writes twice the count of the ``count`` stage, plus ``add``, as text."""

    name = "double"
    config_class = DoubleConfig
    suffix = ".txt"
    reads = ("count",)

    def resolve(
        self,
        config: DoubleConfig,
        model: str,
        model_settings: Mapping[str, Any],
        directory: Path | None,
    ) -> DoubleConfig:
        """Return the settings as given."""
        return config

    def settings(self, config: DoubleConfig) -> dict[str, Any]:
        """Return the record of a double's settings."""
        return config.model_dump(mode="json")

    def check(
        self, config: DoubleConfig, releases: pd.DataFrame, directory: Path | None
    ) -> list[tuple[str, str | None, str]]:
        """Find nothing."""
        return []

    def run(self, config: DoubleConfig, sim: Simulation, path: Path | UPath) -> None:
        """Write twice what the ``count`` stage wrote for *sim*, plus ``add``, to *path*."""
        RUNS.append((self.name, sim.release.id, sim.variant.name))
        path.write_text(str(2 * int(sim.path("count").read_text()) + config.add))


class EchoConfig(BaseModel):
    """Settings of :class:`EchoStage`."""

    model_config = ConfigDict(extra="forbid")

    fail: bool = False


class EchoStage:
    """Writes the release id as text; it reads no other stage."""

    name = "echo"
    config_class = EchoConfig
    suffix = ".txt"

    def resolve(
        self,
        config: EchoConfig,
        model: str,
        model_settings: Mapping[str, Any],
        directory: Path | None,
    ) -> EchoConfig:
        """Return the settings as given."""
        return config

    def settings(self, config: EchoConfig) -> dict[str, Any]:
        """Return the record of an echo's settings."""
        return config.model_dump(mode="json")

    def check(
        self, config: EchoConfig, releases: pd.DataFrame, directory: Path | None
    ) -> list[tuple[str, str | None, str]]:
        """Find nothing."""
        return []

    def run(self, config: EchoConfig, sim: Simulation, path: Path | UPath) -> None:
        """Write *sim*'s release id to *path*, or raise with ``fail``."""
        RUNS.append((self.name, sim.release.id, sim.variant.name))
        if config.fail:
            raise RuntimeError("the echo was told to fail")
        path.write_text(sim.release.id)


class Forgetful:
    """A stage whose config class does not read back what ``settings`` wrote."""

    name = "forgetful"
    config_class = DoubleConfig
    suffix = ".txt"

    def settings(self, config: DoubleConfig) -> dict[str, Any]:
        """Return a record that leaves ``add`` out, so reading it back gives the default."""
        return {"add": 0} if config.add else {"add": 1}


def chain(**count: Any) -> dict[str, dict[str, Any]]:
    """Return a ``stages`` block with both toy stages, ``double`` written first."""
    return {"double": {"class": DOUBLE}, "count": {"class": COUNT, **count}}


class CellsConfig(BaseModel):
    """Settings of :class:`CellsStage`: none."""

    model_config = ConfigDict(extra="forbid")


class CellsStage:
    """Writes the sum of every grid of a concentration result, as text: a stage that reads a type other than particles."""

    name = "cells"
    config_class = CellsConfig
    suffix = ".txt"
    result = "concentration"

    def resolve(
        self,
        config: CellsConfig,
        model: str,
        model_settings: Mapping[str, Any],
        directory: Path | None,
    ) -> CellsConfig:
        """Return *config* as given."""
        return config

    def settings(self, config: CellsConfig) -> dict[str, Any]:
        """Return the record of the stage's settings."""
        return config.model_dump(mode="json")

    def check(
        self, config: CellsConfig, releases: pd.DataFrame, directory: Path | None
    ) -> list[tuple[str, str | None, str]]:
        """List nothing."""
        return []

    def run(self, config: CellsConfig, sim: Simulation, path: Path | UPath) -> None:
        """Write the sum of every variable of every grid of *sim*'s concentration result to *path*."""
        RUNS.append((self.name, sim.release.id, sim.variant.name))
        tree = sim.concentration
        total = sum(
            float(node.to_dataset()[var].sum())
            for node in tree.subtree
            for var in node.data_vars
        )
        path.write_text(str(total))

"""
The dry deposition transform: weigh each row by the tracer that survives to the release.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from runtraj.transforms._common import step_seconds, weigh
from runtraj.transforms.lifetime import survival

if TYPE_CHECKING:
    from runtraj.releases import Release

#: The longest time step, in seconds, the surviving fraction is computed over.
#: A particle can cross the surface layer within a few minutes, so rows
#: further apart than HYSPLIT's one-minute step do not say how long it
#: stayed below the layer top.
MAX_STEP_SECONDS = 60.0


def _hysplit_surface_layer(internal_grid: tuple[int, float, float, float]) -> float:
    """
    Return the depth in meters of HYSPLIT's surface layer, for the internal grid a run printed.

    It is the height of the highest internal level at or below 75 m
    (``metlvl.f``), where level ``k`` is ``aa k**2 + bb k + cc``: 50 m
    on HRRR, 75 m on HYSPLIT's default grid.

    Examples
    --------
    >>> _hysplit_surface_layer((41, 15.0, -5.0, 0.0))
    50.0
    >>> _hysplit_surface_layer((25, 30.0, -25.0, 5.0))
    75.0
    """
    _, aa, bb, cc = internal_grid
    dist = bb * bb - 4.0 * aa * (cc - 75.0)
    k = int((-bb + math.sqrt(dist)) / (2.0 * aa)) if dist >= 0 else 1
    depth = aa * k * k + bb * k + cc
    if not depth > 0:
        raise ValueError(
            f"The internal grid {internal_grid} has no level between the ground "
            "and 75 m; give surface_layer_depth in meters."
        )
    return float(depth)


def _surface_layer_depth(particles: pd.DataFrame, depth: float | None) -> float:
    """Return *depth*, or HYSPLIT's surface layer for the internal grid *particles* record."""
    if depth is not None:
        return depth
    grid = particles.attrs.get("internal_grid")
    if grid is None:
        raise ValueError(
            "The particles record no HYSPLIT internal grid, so the depth of the "
            "surface layer is not known. Give surface_layer_depth in meters (it "
            "was 50 by default, HYSPLIT's on HRRR)."
        )
    return _hysplit_surface_layer(grid)


def dry_deposition_survival(
    particles: pd.DataFrame,
    velocity: float,
    surface_layer_depth: float | None = None,
) -> pd.Series:
    r"""
    Return the fraction of a depositing tracer that survives from each row to the release.

    A particle below ``surface_layer_depth`` loses tracer at the rate
    ``velocity / surface_layer_depth``, HYSPLIT's dry deposition rule.
    Each row stands for the time step that ends at it, so its fraction
    includes its own step and every step between it and the release.

    Parameters
    ----------
    particles : pandas.DataFrame
        The particle table, with ``particle``, ``age`` (seconds) and
        ``zagl`` (m) columns and a row at every time step (HYSPLIT's ``outdt: 0``).
    velocity : float
        The dry deposition velocity, in m/s.
    surface_layer_depth : float, optional
        The depth of the layer the tracer deposits from, in meters above
        ground. Unset, it is HYSPLIT's surface layer, the highest internal
        level at or below 75 m, of the internal grid the particles record
        (``particles.attrs["internal_grid"]``, as a HYSPLIT run's do).

    Returns
    -------
    pandas.Series
        The surviving fraction, between 0 and 1, aligned with
        ``particles``' index.

    Raises
    ------
    ValueError
        If a column is missing, a particle's rows are more than
        :data:`MAX_STEP_SECONDS` apart, or *surface_layer_depth* is unset and
        the particles record no internal grid.

    Notes
    -----
    The fraction is :math:`\exp(-\sum v_d \, \Delta t / H)` over the
    steps spent below :math:`H`, where a step's height is its row's
    ``zagl``: :func:`survival` with the rate :math:`v_d / H` below
    :math:`H` and zero above. It works for any species with a constant deposition
    velocity; use it directly when you want the fraction itself, not a
    weight.

    Examples
    --------
    >>> import pandas as pd
    >>> from runtraj.transforms import dry_deposition_survival
    >>> p = pd.DataFrame(
    ...     {
    ...         "particle": [1, 1, 1],
    ...         "age": [-60.0, -120.0, -180.0],
    ...         "zagl": [10.0, 100.0, 10.0],
    ...     }
    ... )
    >>> dry_deposition_survival(p, velocity=0.01, surface_layer_depth=50.0).round(
    ...     4
    ... ).tolist()
    [0.9881, 0.9881, 0.9763]
    """
    missing = [c for c in ("particle", "age", "zagl") if c not in particles.columns]
    if missing:
        raise ValueError(
            f"Particle DataFrame has no {missing} column(s), required for "
            "dry deposition."
        )
    step = step_seconds(particles)
    if (step > MAX_STEP_SECONDS + 1e-6).any():
        raise ValueError(
            "Dry deposition needs a particle row at every one-minute time step; "
            f"these rows are up to {step.max():g} seconds apart. Have the "
            "transport model write a row every minute (HYSPLIT: outdt: 0)."
        )
    depth = _surface_layer_depth(particles, surface_layer_depth)
    below = particles["zagl"].to_numpy(dtype=float) < depth
    return survival(particles, np.where(below, velocity / depth, 0.0))


class DryDeposition(BaseModel):
    """
    Transform that weighs each row by the tracer that survives dry deposition.

    Use it for a species that deposits to the ground, such as a soluble gas
    or a fine aerosol. Each row's ``weight`` is multiplied by
    :func:`dry_deposition_survival`, the fraction of tracer emitted there
    that reaches the release.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["dry_deposition"] = "dry_deposition"
    velocity: float = Field(gt=0, description="Dry deposition velocity, in m/s.")
    surface_layer_depth: float | None = Field(
        None,
        gt=0,
        description=(
            "Depth of the layer the tracer deposits from, in meters above "
            "ground. Unset, it is HYSPLIT's: the highest internal level at or "
            "below 75 m of the run's internal grid, 50 m on HRRR. Particles "
            "that record no internal grid need it set."
        ),
    )

    def apply(
        self,
        particles: pd.DataFrame,
        release: Release | None = None,
        directory: str | Path | None = None,
    ) -> pd.DataFrame:
        """Return the particles with ``weight`` scaled by the surviving fraction."""
        survival = dry_deposition_survival(
            particles, self.velocity, self.surface_layer_depth
        )
        return weigh(particles, survival.to_numpy())

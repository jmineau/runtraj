"""
The dry deposition transform: weigh each row by the tracer that survives to the release.
"""

from __future__ import annotations

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


def dry_deposition_survival(
    particles: pd.DataFrame,
    velocity: float,
    surface_layer_depth: float = 50.0,
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
    surface_layer_depth : float, default 50.0
        The depth of the layer the tracer deposits from, in meters above
        ground.

    Returns
    -------
    pandas.Series
        The surviving fraction, between 0 and 1, aligned with
        ``particles``' index.

    Raises
    ------
    ValueError
        If a column is missing or a particle's rows are more than
        :data:`MAX_STEP_SECONDS` apart.

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
    >>> dry_deposition_survival(p, velocity=0.01).round(4).tolist()
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
    below = particles["zagl"].to_numpy(dtype=float) < surface_layer_depth
    return survival(particles, np.where(below, velocity / surface_layer_depth, 0.0))


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
    surface_layer_depth: float = Field(
        50.0,
        gt=0,
        description=(
            "Depth of the layer the tracer deposits from, in meters above "
            "ground. HYSPLIT uses its lowest internal level at or below 75 m, "
            "50 m on HRRR."
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

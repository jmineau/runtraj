"""
Loss along the trajectory: the surviving fraction for any rate, and the first-order lifetime transform.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
import pandas as pd
from numpy.typing import ArrayLike
from pydantic import BaseModel, ConfigDict, Field

from runtraj.transforms._common import (
    elapsed_minutes,
    step_minutes,
    trajectory_columns,
    weigh,
)

if TYPE_CHECKING:
    from runtraj.releases import Release


def survival(particles: pd.DataFrame, rate: ArrayLike) -> pd.Series:
    r"""
    Return the fraction of a tracer that survives from each row to the release.

    Use it for a loss whose rate is known at every row and does not depend
    on how much tracer the particle carries: a reaction with an oxidant,
    deposition, rain-out, or a lifetime from your own chemistry model. The
    fraction emitted at a row is the fraction that reaches the release, so
    a row's weight times it is still that row's share at the release.

    Parameters
    ----------
    particles : pandas.DataFrame
        The particle table, with ``particle`` and ``age`` (minutes) columns.
        A table of several simulations tells each trajectory apart by its
        ``release``, ``variant`` and ``realization`` as well.
    rate : array-like
        The loss rate at each row, in s⁻¹, aligned with ``particles``. Zero
        where there is no loss.

    Returns
    -------
    pandas.Series
        The surviving fraction, between 0 and 1, aligned with
        ``particles``' index.

    Raises
    ------
    ValueError
        If a column is missing, ``rate`` has the wrong length, or a rate is
        negative, infinite or missing, or an age is missing.

    Notes
    -----
    The fraction at a row is :math:`\exp(-\sum k \, \Delta t)` over the
    steps from the release to that row, where each row stands for the step
    that ends at it (a particle's first step runs from the release) and :math:`k` is that row's
    rate. A rate that changes slowly between rows, such as one set by
    temperature or an oxidant field, needs no particular step; a rate that
    switches on and off within a step, such as deposition below a fixed
    height, needs short steps (:func:`dry_deposition_survival` asks for
    one-minute rows).

    Examples
    --------
    >>> import pandas as pd
    >>> from runtraj.transforms import survival
    >>> p = pd.DataFrame({"particle": [1, 1, 1], "age": [-60.0, -120.0, -180.0]})
    >>> survival(p, rate=[1e-4, 1e-4, 0.0]).round(4).tolist()
    [0.6977, 0.4868, 0.4868]
    """
    k = np.asarray(rate, dtype=float).ravel()
    if k.shape != (len(particles),):
        raise ValueError(
            f"rate has {k.size} values for {len(particles)} particle rows."
        )
    if not np.isfinite(k).all() or (k < 0).any():
        raise ValueError(
            "rate must be a finite number, zero or positive, at every row."
        )
    step = step_minutes(particles)
    # Add the losses up from the release, one trajectory at a time.
    keys = trajectory_columns(particles)
    rows = particles[keys].reset_index(drop=True)
    rows = rows.assign(
        _loss=k * step.to_numpy() * 60.0,
        _elapsed=elapsed_minutes(particles),
    ).sort_values([*keys, "_elapsed"], kind="stable")
    cumulative = rows.groupby(keys, sort=False, dropna=False)["_loss"].cumsum()
    return pd.Series(np.exp(-cumulative.sort_index().to_numpy()), index=particles.index)


class FirstOrderLifetime(BaseModel):
    """
    Transform that decays each row's ``weight`` by ``exp(-age / lifetime)``.

    ``age`` is the particle's time since release, its ``age`` column
    (minutes), and the lifetime is the species' e-folding lifetime.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["first_order_lifetime"] = "first_order_lifetime"
    lifetime_hours: float = Field(gt=0, description="E-folding lifetime, in hours.")

    def apply(
        self,
        particles: pd.DataFrame,
        release: Release | None = None,
        directory: str | Path | None = None,
    ) -> pd.DataFrame:
        """Return the particles with ``weight`` decayed by age."""
        age_hours = elapsed_minutes(particles) / 60.0
        return weigh(particles, np.exp(-age_hours / self.lifetime_hours))

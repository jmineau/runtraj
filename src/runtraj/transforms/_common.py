"""
What the transforms share: the weight column, a particle's value of a column at release, and the length of each time step.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.typing import ArrayLike

#: The particle column the transforms multiply: how much each row counts.
WEIGHT = "weight"


def weigh(particles: pd.DataFrame, factor: ArrayLike) -> pd.DataFrame:
    """
    Return a copy of the particles with ``weight`` multiplied by *factor*.

    A table without a ``weight`` column starts from 1 at every row.

    Parameters
    ----------
    particles : pandas.DataFrame
        The particle table.
    factor : array-like
        The factor for each row, aligned with ``particles``.

    Returns
    -------
    pandas.DataFrame
        A new table; ``particles`` is unchanged.

    Examples
    --------
    >>> import pandas as pd
    >>> from runtraj.transforms import weigh
    >>> p = pd.DataFrame({"particle": [1, 2]})
    >>> weigh(weigh(p, [0.5, 1.0]), [0.5, 0.5])["weight"].tolist()
    [0.25, 0.5]
    """
    out = particles.copy()
    start = out[WEIGHT].to_numpy(dtype=float) if WEIGHT in out.columns else 1.0
    out[WEIGHT] = start * np.asarray(factor, dtype=float)
    return out


def release_coordinate(particles: pd.DataFrame, coordinate: str) -> pd.Series:
    """
    Return each particle's value of ``coordinate`` at release, indexed by ``particle``.

    Some columns, such as ``release_height``, are constant along a trajectory. Others,
    such as ``pres``, change at every time step. The release value is taken
    from the row nearest the release time (smallest ``|time|``), or from the
    first row when there is no numeric ``age`` column.
    """
    p = particles
    if "age" in p.columns and pd.api.types.is_numeric_dtype(p["age"]):
        ordered = p.assign(_age=p["age"].abs()).sort_values("_age", kind="stable")
    else:
        ordered = p
    first = ordered.drop_duplicates(subset="particle")
    return pd.Series(
        first[coordinate].to_numpy(dtype=float), index=first["particle"].to_numpy()
    )


def step_minutes(particles: pd.DataFrame) -> pd.Series:
    """
    Return the length of the time step that ends at each row, in minutes.

    A particle's rows are ordered by elapsed time (``|age|``); its first
    row's step runs from the release (age 0).

    Parameters
    ----------
    particles : pandas.DataFrame
        The particle table, with ``particle`` and ``age`` (minutes) columns.

    Returns
    -------
    pandas.Series
        The step length, aligned with ``particles``' index.
    """
    missing = [c for c in ("particle", "age") if c not in particles.columns]
    if missing:
        raise ValueError(f"Particle DataFrame has no {missing} column(s).")
    elapsed = particles["age"].abs().astype(float)
    order = elapsed.groupby(particles["particle"]).rank(method="first")
    rows = pd.DataFrame(
        {"particle": particles["particle"], "elapsed": elapsed, "order": order}
    ).sort_values(["particle", "order"])
    step = rows.groupby("particle")["elapsed"].diff().fillna(rows["elapsed"])
    return step.reindex(particles.index)

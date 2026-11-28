"""
What the transforms share: the weight column, a particle's value of a column at release, the length of each time step, and how a table of several simulations is split.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
from numpy.typing import ArrayLike

#: The particle column the transforms multiply: how much each row counts.
WEIGHT = "weight"


#: The columns that tell one simulation's particles from another's in a table
#: that holds several, as ``Project.particles`` returns. Particle numbers
#: start again in each simulation.
SIMULATION_COLUMNS = ("release", "variant", "realization")


def trajectory_columns(particles: pd.DataFrame) -> list[str]:
    """
    Return the columns that name one particle's trajectory in *particles*.

    These are the simulation columns the table has (``release``,
    ``variant``, ``realization``), then ``particle``. A table of one
    simulation, which has none of them, gives ``["particle"]``.
    """
    return [c for c in SIMULATION_COLUMNS if c in particles.columns] + ["particle"]


def per_simulation(
    particles: pd.DataFrame, func: Callable[[pd.DataFrame], pd.DataFrame]
) -> pd.DataFrame:
    """
    Return *func* applied to each simulation's rows of *particles*, in the table's order.

    *func* takes and returns the rows of one simulation, with the same
    index. A table of one simulation is passed to it whole.
    """
    keys = [c for c in SIMULATION_COLUMNS if c in particles.columns]
    if not keys:
        return func(particles)
    groups = particles.groupby(keys, sort=False, dropna=False).indices
    if len(groups) <= 1:
        return func(particles)
    positions = list(groups.values())
    out = pd.concat([func(particles.iloc[rows]) for rows in positions])
    return out.iloc[np.argsort(np.concatenate(positions), kind="stable")]


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


def elapsed_seconds(particles: pd.DataFrame) -> np.ndarray:
    """
    Return each row's time since release, ``|age|``, in seconds.

    Raises
    ------
    ValueError
        If there is no ``age`` column, or a row's is missing.
    TypeError
        If ``age`` is not a number of seconds, such as a timedelta.
    """
    if "age" not in particles.columns:
        raise ValueError("Particle DataFrame has no 'age' column.")
    age = particles["age"]
    if not pd.api.types.is_numeric_dtype(age) or pd.api.types.is_bool_dtype(age):
        raise TypeError(
            f"The particles' age must be seconds since release, as numbers; got {age.dtype}."
        )
    seconds = np.abs(age.to_numpy(dtype=float))
    if np.isnan(seconds).any():
        raise ValueError(
            f"The particles' age is missing in {int(np.isnan(seconds).sum())} row(s)."
        )
    return seconds


def release_coordinate(particles: pd.DataFrame, coordinate: str) -> pd.Series:
    """
    Return each particle's value of ``coordinate`` at release, indexed by ``particle``.

    Some columns, such as ``release_height``, are constant along a trajectory. Others,
    such as ``pres``, change at every time step. The release value is taken
    from the row nearest the release time (smallest ``|time|``), or from the
    first row when there is no numeric ``age`` column.

    *particles* are one simulation's: in a table of several, particle
    numbers repeat. Split it by simulation first.
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


def step_seconds(particles: pd.DataFrame) -> pd.Series:
    """
    Return the length of the time step that ends at each row, in seconds.

    A particle's rows are ordered by elapsed time (``|age|``); its first
    row's step runs from the release (age 0). In a table of several
    simulations, each trajectory is told apart by its ``release``,
    ``variant`` and ``realization`` as well as its ``particle``.

    Parameters
    ----------
    particles : pandas.DataFrame
        The particle table, with ``particle`` and ``age`` (seconds) columns.

    Returns
    -------
    pandas.Series
        The step length, aligned with ``particles``' index.
    """
    missing = [c for c in ("particle", "age") if c not in particles.columns]
    if missing:
        raise ValueError(f"Particle DataFrame has no {missing} column(s).")
    keys = trajectory_columns(particles)
    elapsed = elapsed_seconds(particles)
    rows = particles[keys].reset_index(drop=True).assign(_elapsed=elapsed)
    rows = rows.sort_values([*keys, "_elapsed"], kind="stable")
    step = rows.groupby(keys, sort=False, dropna=False)["_elapsed"].diff()
    step = step.fillna(rows["_elapsed"]).sort_index()
    return pd.Series(step.to_numpy(), index=particles.index)

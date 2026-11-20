"""
The pressure-weighting transform: weight each particle by its share of the column's air.

:func:`particle_pwf` derives the weights from the particles' own
pressures; it is public so your own transforms can use it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from runtraj.releases import Release

from runtraj.transforms._common import per_simulation, release_coordinate, weigh


def particle_pwf(
    particles: pd.DataFrame,
    surface_pressure: float | None = None,
    altitude_ref: Literal["agl", "msl"] = "agl",
) -> tuple[pd.Series, pd.Series]:
    """
    Return each particle's release pressure and pressure weight.

    Following Wu et al. (2018, *Geosci. Model Dev.* 11, 4843), a hypsometric curve
    ``ln p = b + a·z`` is fit to the particles' first-step heights and
    pressures. The fit smooths out the one time step of turbulence between
    release and the first output, and gives a surface pressure when none is
    supplied. It is evaluated at each release height, and the spacing
    between neighboring release pressures gives the air mass each release
    height represents.

    HYSPLIT spreads column particles evenly over height, each placed at
    random within its own slab, one slab per particle. Each release height therefore
    stands for the slab centered on it. Slab edges sit midway between
    neighboring release pressures, the ground closes the bottom slab, and the
    top slab extends above its release height as far as it does below.

    A multipoint release releases several particles from each point. Those
    particles share one release height, so the point's slab is split evenly
    among them.

    Parameters
    ----------
    particles : pandas.DataFrame
        One simulation's particle table, with ``particle``, ``pres`` (hPa), and ``zagl`` (m), and
        optionally ``release_height``, the release height (m). Without ``release_height`` the
        first-step height is used. An MSL release also needs ``zsfc``, the
        terrain height (m above sea level).
    surface_pressure : float, optional
        Surface pressure in hPa. Defaults to the fitted curve at the ground.
    altitude_ref : {"agl", "msl"}, default "agl"
        Vertical reference of the release heights, ``release.altitude_ref``.
        With ``"msl"`` the fit is made against ``zagl + zsfc`` so that it can
        be evaluated at the release heights, and the ground closing the
        bottom slab is the terrain under the lowest release height.

    Returns
    -------
    xpres : pandas.Series
        Release pressure of each particle in hPa, indexed by ``particle``.
    pwf : pandas.Series
        Pressure weight of each particle, indexed by ``particle``. The weights
        sum to the fraction of the atmosphere's mass inside the column,
        ``(p_sfc - p_top) / p_sfc``. The rest lies above the column top,
        where surface fluxes do not reach the release.
    """
    for col in ("pres", "zagl"):
        if col not in particles.columns:
            raise ValueError(
                f"Pressure weighting requires the {col!r} particle variable; "
                "run the transport model with it (HYSPLIT: add it to varsiwant)."
            )
    pres = release_coordinate(particles, "pres")
    zagl = release_coordinate(particles, "zagl")
    missing = int((pres.isna() | zagl.isna()).sum())
    if missing:
        raise ValueError(
            f"Pressure weighting reads each particle's first pres and zagl, and "
            f"{missing} of {len(pres)} particles have none there."
        )

    # Particle heights in the release's own vertical reference, so the fit
    # can be evaluated at the release heights.
    if altitude_ref == "msl":
        if "zsfc" not in particles.columns:
            raise ValueError(
                "Pressure weighting for a release with altitude_ref='msl' "
                "requires the 'zsfc' particle variable; run the transport model "
                "with it (HYSPLIT: add it to varsiwant)."
            )
        zsfc = release_coordinate(particles, "zsfc")
        z_fit = zagl + zsfc
    else:
        zsfc = None
        z_fit = zagl
    z_release = (
        release_coordinate(particles, "release_height")
        if "release_height" in particles.columns
        else z_fit
    )

    heights = np.unique(z_release.to_numpy())  # distinct release heights, ascending
    if len(heights) < 2 or z_fit.nunique() < 2:
        raise ValueError(
            "Pressure weighting needs particles released over a range of heights "
            "(a ColumnRelease); all particles share one release height."
        )
    a, b = np.polyfit(z_fit.to_numpy(), np.log(pres.to_numpy()), 1)
    if a >= 0:
        raise ValueError(
            "Could not fit a pressure profile to the particles (pressure does "
            "not decrease with height)."
        )

    # The ground closes the bottom slab: z = 0 above ground, or the terrain
    # under the lowest release height above sea level.
    z_ground = 0.0
    if zsfc is not None:
        at_bottom = z_release.to_numpy() == heights[0]
        z_ground = float(np.median(zsfc.to_numpy()[at_bottom]))
    p_sfc = (
        float(surface_pressure)
        if surface_pressure is not None
        else float(np.exp(b + a * z_ground))
    )

    xpres = pd.Series(
        p_sfc * np.exp(a * (z_release.to_numpy() - z_ground)), index=z_release.index
    )

    # One slab per distinct release height, surface upward, shared evenly by
    # the particles released at that height.
    levels = p_sfc * np.exp(a * (heights - z_ground))
    mids = (levels[:-1] + levels[1:]) / 2.0
    lower_edges = np.concatenate(([p_sfc], mids))
    upper_edges = np.concatenate((mids, [2 * levels[-1] - mids[-1]]))
    slab = (lower_edges - upper_edges) / p_sfc
    level = np.searchsorted(heights, z_release.to_numpy())
    count = np.bincount(level, minlength=len(heights))
    pwf = pd.Series(slab[level] / count[level], index=z_release.index)
    return xpres, pwf


class PressureWeighting(BaseModel):
    """
    Transform that weights each particle by its share of the column's air mass.

    The weights come from the particles' own first-step heights and
    pressures (see :func:`particle_pwf`). They are multiplied by the
    number of particles, so a mean over the particles, as a stage takes,
    does not change with the number of particles. The particles of a multipoint release share their point's
    weight evenly. Adds ``xpres`` (release pressure, hPa) and ``pwf``
    columns. Requires ``pres`` and ``zagl`` among the particle variables (both
    in HYSPLIT's default ``varsiwant``), and ``zsfc`` for a release with
    ``altitude_ref="msl"``.
    """

    model_config = ConfigDict(frozen=True)

    #: It reads ``zsfc`` for a release above sea level, so a stage's check
    #: can say when HYSPLIT's ``varsiwant`` lacks it.
    reads_zsfc: ClassVar[bool] = True

    kind: Literal["pressure_weighting"] = "pressure_weighting"
    surface_pressure: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Surface pressure at the bottom of the column, in hPa. Unset "
            "estimates it from the particles."
        ),
    )

    def apply(
        self,
        particles: pd.DataFrame,
        release: Release | None = None,
        directory: str | Path | None = None,
    ) -> pd.DataFrame:
        """
        Return the particles with ``weight`` multiplied by the pressure weight.

        The release heights are in the release's ``altitude_ref``, and
        taken to be above ground without a release. A table of several
        simulations is weighted one simulation at a time.
        """
        altitude_ref = release.altitude_ref if release is not None else "agl"

        def one(rows: pd.DataFrame) -> pd.DataFrame:
            xpres, pwf = particle_pwf(rows, self.surface_pressure, altitude_ref)
            out = rows.copy()
            ids = out["particle"].to_numpy()
            out["xpres"] = xpres.reindex(ids).to_numpy()
            out["pwf"] = pwf.reindex(ids).to_numpy()
            return weigh(out, out["pwf"].to_numpy() * len(pwf))

        return per_simulation(particles, one)

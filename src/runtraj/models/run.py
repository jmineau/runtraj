"""Run a transport model for one release, or many, and finish its result."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from pydantic import BaseModel

from runtraj.exceptions import SimulationError
from runtraj.meteorology import run_window
from runtraj.particles import (
    add_release_heights,
    check_particles,
)
from runtraj.releases import releases_to_frame
from runtraj.results import model_result, result_type

if TYPE_CHECKING:
    from runtraj.releases import Release
from runtraj.models.base import ModelRun, TransportConfig, TransportModel
from runtraj.models.registry import get_model


def run_model(
    name: str,
    release: Release,
    config: TransportConfig,
    met: Any,
    workdir: Path | None = None,
    timeout: int | None = None,
) -> ModelRun:
    """
    Run the transport model called *name* for one release, and finish its result.

    The result is checked against its type (:mod:`runtraj.results`).
    Particles are checked against the particle table
    (:func:`runtraj.particles.check_particles`) and get their release heights
    (:func:`runtraj.particles.add_release_heights`). These steps are
    runtraj's, the same for any model. Nothing else is added: ``foot`` is
    the model's own, or missing for a model that writes none.

    Parameters
    ----------
    name : str
        The transport model (:data:`MODELS`).
    release : Release
        Where and when particles are released.
    config : TransportConfig
        The model's config.
    met : BaseModel
        The meteorology, of the model's ``met_config_class``.
    workdir : Path, optional
        An empty directory for the run's files. ``None`` lets the model make
        its own.
    timeout : int, optional
        Time limit in seconds.

    Raises
    ------
    SimulationError
        If the model's ``check_releases`` refuses the release
        (``RELEASE_REFUSED``), before it runs; if the run fails, as when the
        meteorology does not cover it (``MET_COVERAGE``), or the model wrote
        no result (``NO_PARTICLE_DATA``, ``NO_CONCENTRATION_DATA``); or if where the particles of a column or multipoint
        release started is not known (``NO_RELEASE_ROWS``).
    """
    return _run_one(get_model(name), release, config, met, workdir, timeout)


def _run_one(
    model: TransportModel,
    release: Release,
    config: TransportConfig,
    met: Any,
    workdir: Path | None,
    timeout: int | None,
) -> ModelRun:
    """Run *model* for *release* as :func:`run_model` does: refuse, run, finish."""
    refused = _refused(model, [release], config).get(str(release.id))
    if refused is not None:
        raise refused
    window = run_window(release.time, config.n_hours)
    run = _returned(model.run(release, config, met, window, workdir, timeout=timeout))
    return _finish(run, release, model, config)


def run_model_many(
    name: str,
    releases: list[Release],
    config: TransportConfig,
    met: Any,
    workdir: Path | None = None,
    timeout: int | None = None,
) -> dict[str, ModelRun | SimulationError]:
    """
    Run a batched transport model once for many releases, and finish each one's particles.

    The model's one table is split by its ``release`` column, and each
    release's particles get the steps :func:`run_model` applies. A release
    the model's ``check_releases`` refuses is left out of the call and
    gets a ``RELEASE_REFUSED`` error; one whose particles are missing or
    fail those steps gets its error, and the others their runs.

    Returns
    -------
    dict
        ``{release id: ModelRun}``, or the :class:`SimulationError` of a
        release that failed.

    Raises
    ------
    SimulationError
        If the whole call fails.
    """
    return _run_many(get_model(name), releases, config, met, workdir, timeout)


def _run_many(
    model: TransportModel,
    releases: list[Release],
    config: TransportConfig,
    met: Any,
    workdir: Path | None,
    timeout: int | None,
) -> dict[str, ModelRun | SimulationError]:
    """Run batched *model* for *releases* as :func:`run_model_many` does."""
    made = model_result(model, config)
    if made != "particles":
        raise TypeError(
            f"Transport model {model.name!r} is batched and makes {made}: a "
            "batched model returns one particle table for many releases, so it "
            "makes particles."
        )
    results: dict[str, ModelRun | SimulationError] = dict(
        _refused(model, releases, config)
    )
    releases = [r for r in releases if str(r.id) not in results]
    if not releases:
        return results
    windows: list[tuple[dt.datetime, dt.datetime]] = [
        run_window(r.time, config.n_hours) for r in releases
    ]
    run = _returned(
        model.run_many(releases, config, met, windows, workdir, timeout=timeout)
    )
    if not isinstance(run.result, pd.DataFrame) or "release" not in run.result.columns:
        raise SimulationError(
            "A batched transport model's particles need a release column.",
            reason="NO_PARTICLE_DATA",
            log=run.log,
        )
    by_release = dict(tuple(run.result.groupby("release", sort=False)))
    for release in releases:
        rows = by_release.get(str(release.id))
        own = replace(
            run,
            result=(
                pd.DataFrame()
                if rows is None
                else rows.drop(columns="release").reset_index(drop=True)
            ),
        )
        try:
            results[str(release.id)] = _finish(own, release, model, config)
        except SimulationError as error:
            error.log = run.log
            results[str(release.id)] = error
    return results


def _returned(run: Any) -> ModelRun:
    """Return *run*, what a model's ``run`` or ``run_many`` returned, when it is a :class:`ModelRun`."""
    if not isinstance(run, ModelRun):
        raise TypeError(
            f"A transport model's run must return a ModelRun, not {type(run).__name__}."
        )
    return run


def _release_points(releases: Iterable[Release]) -> pd.DataFrame:
    """
    Return *releases* as a model's ``check_releases`` gets them: one row per release point.

    The columns are ``release`` (the release id), ``kind``, ``time``,
    ``longitude``, ``latitude``, ``altitude`` and ``altitude_ref``, the
    columns of a project's release rows that a model reads.
    """
    releases = list(releases)
    rows = releases_to_frame(releases)
    each = [r for r in releases for _ in r.coords()]  # a release per point
    rows["release"] = [str(r.id) for r in each]
    rows["kind"] = [r.kind for r in each]
    return rows[
        [
            "release",
            "kind",
            "time",
            "longitude",
            "latitude",
            "altitude",
            "altitude_ref",
        ]
    ]


def _refused(
    model: TransportModel, releases: list[Release], config: TransportConfig
) -> dict[str, SimulationError]:
    """Return the error of each of *releases* the model's ``check_releases`` refuses, by release id."""
    ids = {str(r.id) for r in releases}
    table = model.check_releases(_release_points(releases), config)
    return {
        str(release): SimulationError(str(message), reason="RELEASE_REFUSED")
        for _check, release, message in table.itertuples(index=False)
        if str(release) in ids
    }


def _finish(
    run: ModelRun, release: Release, model: TransportModel, config: TransportConfig
) -> ModelRun:
    """
    Return *run* with its result checked, and particles with their release heights.

    Particles that stop before the end of the run are kept: the transport
    model fails a run cut short by its meteorology itself, and particles
    that all left the met's domain are a complete run. The particles keep
    the columns the model wrote: ``foot`` is the model's own, or missing.
    Another type of result is checked by its type (:mod:`runtraj.results`).

    Raises
    ------
    SimulationError
        With the run's log, if the model wrote no particles
        (``NO_PARTICLE_DATA``), or another type of result that its type
        refuses (``NO_CONCENTRATION_DATA``, ...), or where each particle of
        a column or multipoint release started is not known.
    """
    made = model_result(model, config)
    if made != "particles":
        try:
            result_type(made).check(run.result)
        except ValueError as error:
            raise SimulationError(
                str(error), reason=f"NO_{made.upper()}_DATA", log=run.log
            ) from error
        return run
    if not isinstance(run.result, pd.DataFrame) or run.result.empty:
        raise SimulationError(
            "The transport model wrote no particles.",
            reason="NO_PARTICLE_DATA",
            log=run.log,
        )
    check_particles(run.result)
    try:
        rows = model.release_rows
        particles = add_release_heights(
            run.result, release, None if rows is None else partial(rows, config=config)
        )
    except ValueError as error:
        raise SimulationError(
            str(error), reason="NO_RELEASE_ROWS", log=run.log
        ) from error
    return replace(run, result=particles)


def run_trajectories(
    release: Release,
    met: BaseModel | Mapping[str, Any],
    *,
    model: str,
    workdir: str | Path | None = None,
    timeout: int | None = None,
    **params: Any,
) -> Any:
    """
    Run the transport model for one release and return what it made, particles unless the settings say another type.

    This is what a project runs for each release, without a project: the
    result is returned, not stored. Its type follows the settings, as a
    variant's does (:mod:`runtraj.results`): particles, or with HYSPLIT's
    ``mode="trajectory"`` the trajectory table, or with
    ``mode="concentration"`` the grids.

    Parameters
    ----------
    release : Release
        Where and when particles are released.
    met : str, dict or BaseModel
        The meteorology, as one entry under ``mets:`` in ``config.yaml``,
        checked by the model's met config
        (:class:`runtraj.models.hysplit.HysplitMetConfig` for HYSPLIT). A string is
        the folder of its files, and a relative folder starts from the
        working directory.
    model : str
        The transport model: one runtraj supports (:data:`MODELS`, such as
        ``"hysplit"``) or the import path of a model class. There is no
        default.
    workdir : str or Path, optional
        Directory the model runs in, kept afterwards for a look at its
        files. It must be empty or not exist yet. Without it the model runs
        in a temporary directory that is removed.
    timeout : int, optional
        Time limit in seconds. ``None`` waits indefinitely.
    **params
        The transport model's parameters, such as ``n_hours`` and
        HYSPLIT's ``numpar`` (:class:`runtraj.models.hysplit.HysplitConfig`).
        ``n_hours`` is required; the others default to the model's own
        values.

    Returns
    -------
    pandas.DataFrame or xarray.DataTree
        The particle table, one row per particle per output step, with
        ``time`` (UTC), the release time plus ``age``, as a table read from
        a particle file has. ``foot`` is the model's own (HYSPLIT's, made
        with ``veght``). A model that writes no ``foot`` returns none.
        With settings that make another type, that type's result: the
        trajectory table (:mod:`runtraj.trajectories`), or a tree of
        concentration grids (:mod:`runtraj.concentration`).

    Raises
    ------
    ValueError
        If *workdir* is not empty, or a parameter is not one of the
        model's.
    SimulationError
        If the run fails; ``reason`` says why.
    MeteorologyError
        If the met files the run needs are missing.

    Examples
    --------
    >>> release = runtraj.PointRelease(  # doctest: +SKIP
    ...     time="2023-07-15 18:00", longitude=-111.848, latitude=40.766, altitude=10
    ... )
    >>> particles = runtraj.run_trajectories(
    ...     release,
    ...     "/data/hrrr",
    ...     model="hysplit",
    ...     n_hours=-24,
    ...     numpar=200,  # HYSPLIT's particle count
    ... )  # doctest: +SKIP
    >>> table = runtraj.run_trajectories(
    ...     release, "/data/hrrr", model="hysplit", n_hours=-24, mode="trajectory"
    ... )  # doctest: +SKIP
    """
    transport_model = get_model(model)
    config = transport_model.config_class(**params)
    made = model_result(transport_model, config)
    met_config = transport_model.met_config_class.model_validate(met)
    if workdir is not None:
        workdir = Path(workdir)
        if workdir.exists() and any(workdir.iterdir()):
            raise ValueError(
                f"{workdir} is not empty. The model would read files left from "
                "another run; give an empty or new directory."
            )
        workdir.mkdir(parents=True, exist_ok=True)
    particles = run_model(model, release, config, met_config, workdir, timeout).result
    if made != "particles":
        return particles
    particles["time"] = pd.Timestamp(release.time) + pd.to_timedelta(
        particles["age"].to_numpy(), unit="min"
    )
    return particles

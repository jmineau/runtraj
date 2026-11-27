"""
The transport model interface.

A transport model turns one release and one set of transport settings into
a result: a particle table, or another type of result
(:mod:`runtraj.results`). HYSPLIT (:class:`runtraj.models.hysplit.HysplitModel`)
and FLEXPART 11 (:class:`runtraj.models.flexpart.FlexpartModel`) are the
models runtraj supports (:data:`MODELS`); a model in another package is named by its import
path. The worker reaches
a model through :func:`get_model` by the name a run's settings record, so
another model needs no change to the worker.
"""

from __future__ import annotations

from runtraj.models.base import (
    RESERVED_SETTINGS,
    MetConfig,
    ModelInfo,
    ModelRun,
    TransportConfig,
    TransportModel,
)
from runtraj.models.registry import MODELS, get_model
from runtraj.models.run import run_model, run_model_many, run_trajectories

__all__ = [
    "MODELS",
    "ModelInfo",
    "MetConfig",
    "ModelRun",
    "TransportConfig",
    "TransportModel",
    "RESERVED_SETTINGS",
    "get_model",
    "run_model",
    "run_model_many",
    "run_trajectories",
]

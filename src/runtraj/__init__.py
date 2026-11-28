"""
runtraj runs particle dispersion models at scale.

:class:`Project` runs many releases, each under one or more variants of the
settings, from a directory of releases and settings, and loads their
particles. :func:`run_trajectories` runs the transport model for one release,
without a project.
"""

from .config import ProjectConfig
from .exceptions import RuntrajError
from .execution import ExecutionConfig, task_share
from .identity import runtraj_version
from .models import run_trajectories
from .particles import read_particles
from .project import Project
from .releases import (
    ColumnRelease,
    MultiPointRelease,
    PointRelease,
    Release,
    read_releases,
)
from .simulation import Simulation
from .spatial import Bounds

__version__ = runtraj_version()

__all__ = [
    # A project and its settings
    "Project",
    "ProjectConfig",
    "ExecutionConfig",
    # Releases
    "Release",
    "PointRelease",
    "ColumnRelease",
    "MultiPointRelease",
    "read_releases",
    # One simulation of a project
    "Simulation",
    # Boxes
    "Bounds",
    # One release, without a project
    "run_trajectories",
    "task_share",
    # Result files
    "read_particles",
    # Exceptions (all of them live in runtraj.exceptions)
    "RuntrajError",
]

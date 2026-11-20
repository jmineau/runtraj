"""
The particle table: its schema, particle files, and release heights.

The functions are in :mod:`~runtraj.particles.table`, and the methods of
``particles.runtraj`` in :mod:`~runtraj.particles.accessor`.
"""

from runtraj.particles.accessor import ParticlesAccessor
from runtraj.particles.table import (
    PARTICLE_SCHEMA,
    ParticleMetadata,
    add_release_heights,
    check_particles,
    particles_from_table,
    particles_metadata,
    read_particles,
    release_metadata,
    stored_release,
    write_particles,
)

__all__ = [
    "PARTICLE_SCHEMA",
    "ParticleMetadata",
    "ParticlesAccessor",
    "add_release_heights",
    "check_particles",
    "particles_from_table",
    "particles_metadata",
    "read_particles",
    "release_metadata",
    "stored_release",
    "write_particles",
]

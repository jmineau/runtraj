"""
FLEXPART 11 as a transport model.

``model: flexpart`` runs the executable of a FLEXPART 11 checkout you built
(:class:`FlexpartConfig`'s ``directory``) on GFS or ERA5 GRIB files
(:class:`FlexpartMetConfig`). runtraj never ships FLEXPART, which is GPL-3.
:func:`read_partoutput` reads FLEXPART's particle output on its own, for a
run made outside runtraj.
"""

from runtraj.models.flexpart.config import (
    FlexpartConfig,
    FlexpartMetConfig,
    flexpart_version,
)
from runtraj.models.flexpart.model import FlexpartModel
from runtraj.models.flexpart.output import PARTICLE_COLUMNS, read_partoutput

__all__ = [
    "PARTICLE_COLUMNS",
    "FlexpartConfig",
    "FlexpartMetConfig",
    "FlexpartModel",
    "flexpart_version",
    "read_partoutput",
]

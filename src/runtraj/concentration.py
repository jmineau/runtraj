"""
Concentration grids: what a transport model makes when it counts its particles on a grid.

A run in concentration mode, such as HYSPLIT's ``mode: concentration``,
gives one result per release: a set of grids, each with a variable per
pollutant over ``(time, z, lat, lon)``. It is an :class:`xarray.DataTree`
with one child per grid, named as the grid is in ``config.yaml``, and it
is stored as one NetCDF file per release, a group per grid::

    tree = sim.concentration
    tree["local"].to_dataset()  # the grid called local
    tree["local"]["TRCA"].sel(z=100).sum("time")

Each grid's Dataset has:

- ``time``, when each sample starts (UTC), and ``time_end``, when it ends;
- ``z``, the top of each level in meters above ground, ``0`` for the
  ground, where deposition is counted;
- ``lat`` and ``lon``, the centers of the grid cells, in degrees;
- one variable per pollutant, named by the pollutant, whose ``units`` and
  ``long_name`` attributes say what it holds, which the model and its
  settings decide: for HYSPLIT, mass per cubic meter in the pollutant's own
  mass unit (the unit of its emission rate per hour), and mass per square
  meter at ``z = 0``, or, with ``cmass: 1``, the mass in each cell. Cells
  HYSPLIT did not write are zero.

A model may count each pollutant several times over, apart: HYSPLIT can
keep the mass of each release location, or of each hour of emission, in a
grid of its own (HYSPLIT's ``ichem: 1`` and ``ichem: 10``). The variables then have
one more dimension, before the four: ``source``, numbered from 1 in the
order of the release's locations, or ``release_time``, the UTC time each
part of the emission started.

A transport model reads its own output into such grids (HYSPLIT's
:func:`runtraj.models.hysplit.read_cdump`). :func:`write_concentration` and
:func:`read_concentration` store and read the whole result.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import xarray as xr

from runtraj._paths import atomic_path, location

if TYPE_CHECKING:
    from upath import UPath

    from runtraj.releases import Release

#: The extension of a stored concentration result.
CONCENTRATION_SUFFIX = ".nc"

#: The dimensions of every grid's variables, in order. A variable may have
#: one more before them, one of :data:`SPLIT_DIMS`.
CONCENTRATION_DIMS = ("time", "z", "lat", "lon")

#: The dimensions a grid's variables may have before
#: :data:`CONCENTRATION_DIMS`, when the model counts each pollutant apart
#: by where or when it was released.
SPLIT_DIMS = ("source", "release_time")


def describe_grid(ds: xr.Dataset, units: str, long_name: str) -> xr.Dataset:
    """
    Give a grid's coordinates and variables their units and meanings, in place, and return it.

    A transport model that makes concentration calls it on each grid it
    reads, so that every model's grids say the same about the coordinates.
    What the variables hold depends on the model and its settings, so the
    model says it.

    Parameters
    ----------
    ds : xarray.Dataset
        A grid, with the coordinates of :data:`CONCENTRATION_DIMS` and
        ``time_end``.
    units : str
        The ``units`` attribute of every variable.
    long_name : str
        The ``long_name`` attribute of every variable, where ``{name}`` is
        the variable's own name.

    Examples
    --------
    >>> import numpy as np
    >>> ds = xr.Dataset(
    ...     {"TEST": (CONCENTRATION_DIMS, np.zeros((1, 1, 1, 1)))},
    ...     coords={
    ...         "time": [0],
    ...         "time_end": ("time", [0]),
    ...         "z": [100],
    ...         "lat": [40.0],
    ...         "lon": [-112.0],
    ...     },
    ... )
    >>> describe_grid(ds, "mass m-3", "{name} concentration")["TEST"].attrs["long_name"]
    'TEST concentration'
    """
    ds["time"].attrs = {"long_name": "start of the sample"}
    ds["time_end"].attrs = {"long_name": "end of the sample"}
    ds["z"].attrs = {
        "units": "m",
        "long_name": "top of the level above ground; 0 is the ground, for deposition",
    }
    ds["lat"].attrs = {
        "units": "degrees_north",
        "long_name": "latitude of the cell center",
    }
    ds["lon"].attrs = {
        "units": "degrees_east",
        "long_name": "longitude of the cell center",
    }
    for name in ds.data_vars:
        ds[name].attrs = {"long_name": long_name.format(name=name), "units": units}
    return ds


# -- the result ----------------------------------------------------------------


def check_concentration(result: Any) -> None:
    """
    Raise unless *result* is a concentration result: a DataTree of grids.

    Each child is a grid with the dimensions of :data:`CONCENTRATION_DIMS`,
    at least one sample, and at least one pollutant. Its variables may
    have one dimension more, before those: one of :data:`SPLIT_DIMS`.

    Raises
    ------
    ValueError
        Saying what is wrong.
    """
    if not isinstance(result, xr.DataTree):
        raise ValueError(
            "A concentration result is an xarray.DataTree with a child per "
            f"grid, not {type(result).__name__}."
        )
    if not result.children:
        raise ValueError("A concentration result needs at least one grid.")
    for name, node in result.children.items():
        ds = node.to_dataset()
        missing = [d for d in CONCENTRATION_DIMS if d not in ds.dims]
        if missing:
            raise ValueError(f"Grid {name!r} has no {missing} dimension.")
        if not ds.data_vars:
            raise ValueError(f"Grid {name!r} has no pollutant.")
        if ds.sizes["time"] == 0:
            raise ValueError(f"Grid {name!r} has no sampling time.")
        for var in ds.data_vars.values():
            split = var.dims[: -len(CONCENTRATION_DIMS)]
            if var.dims[len(split) :] != CONCENTRATION_DIMS or (
                split and (len(split) > 1 or split[0] not in SPLIT_DIMS)
            ):
                raise ValueError(
                    f"Grid {name!r}'s {var.name} has dimensions {var.dims}, "
                    f"not {CONCENTRATION_DIMS}, alone or after one of "
                    f"{SPLIT_DIMS}."
                )


def write_concentration(
    path: str | Path | UPath,
    result: xr.DataTree,
    release: Release,
    settings: Mapping[str, Any],
    met_files: list[Path],
    metadata: Mapping[bytes, bytes] | None = None,
) -> Path | UPath:
    """
    Write a concentration result to a NetCDF file that :func:`read_concentration` reads alone.

    Each grid is a group of the file. The release id, the run's settings
    and the met files go in the file's attributes, with *metadata*, such as
    the settings hash.

    Parameters
    ----------
    path : str, Path or UPath
        File to write. On an object store it is written whole, from a
        temporary file here.
    result : xarray.DataTree
        A child per grid (:func:`check_concentration`).
    release : Release
        The release the result is of.
    settings : mapping
        The run's settings, as its folder records them.
    met_files : list of Path
        The met files the run read.
    metadata : mapping, optional
        More attributes, as ``{b"runtraj:...": b"..."}``.

    Raises
    ------
    ImportError
        If ``netCDF4`` is not installed (``pip install runtraj[concentration]``).
    """
    require_netcdf4()
    check_concentration(result)
    tree = result.copy()
    tree.attrs = {
        "release": str(release.id),
        "runtraj:settings": json.dumps(dict(settings)),
        "runtraj:met_files": json.dumps([str(p) for p in met_files]),
        **{k.decode(): v.decode() for k, v in (metadata or {}).items()},
    }
    # Most cells of a grid are zero, so each variable is compressed.
    encoding = {
        node.path: {name: {"zlib": True, "complevel": 4} for name in node.data_vars}
        for node in tree.subtree
        if node.data_vars
    }
    target = location(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(target, Path):
        with atomic_path(target) as tmp:
            tree.to_netcdf(tmp, engine="netcdf4", encoding=encoding)
        return target
    with tempfile.TemporaryDirectory(prefix="runtraj-") as tmp:
        local = Path(tmp) / target.name
        tree.to_netcdf(local, engine="netcdf4", encoding=encoding)
        target.write_bytes(local.read_bytes())
    return target


def read_concentration(path: str | Path | UPath) -> xr.DataTree:
    """
    Read a concentration file :func:`write_concentration` wrote.

    The whole file is read into memory, so the file is closed when this
    returns. Its attributes say which release and settings it is of
    (``release``, ``runtraj:settings``, ``runtraj:met_files``).

    Raises
    ------
    FileNotFoundError
        If there is no file at *path*.
    ImportError
        If ``netCDF4`` is not installed (``pip install runtraj[concentration]``).
    """
    require_netcdf4()
    where = location(path)
    if not where.exists():
        raise FileNotFoundError(f"No concentration file at {where}.")
    if isinstance(where, Path):
        with xr.open_datatree(where, engine="netcdf4") as tree:
            return tree.load()
    with tempfile.TemporaryDirectory(prefix="runtraj-") as tmp:
        local = Path(tmp) / where.name
        local.write_bytes(where.read_bytes())
        with xr.open_datatree(local, engine="netcdf4") as tree:
            return tree.load()


def require_netcdf4() -> None:
    """
    Raise when ``netCDF4``, which writes and reads the files, is not installed.

    A transport model that makes concentration calls it before it runs, so
    that a run is not lost for want of a way to store it.

    Raises
    ------
    ImportError
        Naming the extra that installs it.
    """
    try:
        import netCDF4  # noqa: F401
    except ImportError:
        raise ImportError(
            "A concentration result is a NetCDF file, which needs netCDF4: "
            "pip install 'runtraj[concentration]'."
        ) from None


__all__ = [
    "CONCENTRATION_DIMS",
    "CONCENTRATION_SUFFIX",
    "SPLIT_DIMS",
    "check_concentration",
    "describe_grid",
    "read_concentration",
    "require_netcdf4",
    "write_concentration",
]

Result Types and Concentration
==============================

.. currentmodule:: runtraj

A transport model makes one type of result per run, and every model that
makes a type follows its one schema (:mod:`runtraj.results`):

.. list-table::
   :header-rows: 1
   :widths: 18 52 30

   * - Type
     - What it is
     - File per release
   * - ``particles``
     - The particle table (:doc:`particles`), from HYSPLIT's particles mode
       or another model that follows particles.
     - Parquet, under ``particles/``
   * - ``concentration``
     - Grids of concentration, from HYSPLIT's ``mode: concentration``
       (:doc:`../guides/concentration`).
     - NetCDF, under ``concentration/``
   * - ``trajectories``
     - The trajectory table (:doc:`trajectories`), from HYSPLIT's ``mode:
       trajectory`` (:doc:`../guides/trajectories`).
     - Parquet, under ``trajectories/``

A model says which type a config makes with ``result(config)``
(:class:`~runtraj.models.TransportModel`), and makes particles when it does
not say. :attr:`runtraj.config.Variant.result` is the variant's type, and the
name of its tree in the output directory. ``project.status()`` has a column
for each type a variant makes, and each type has its own read:
:meth:`Project.particles` and ``sim.particles``,
:meth:`Project.concentration` and ``sim.concentration``,
:meth:`Project.trajectories` and ``sim.trajectories``. A stage reads
particles, so a variant that makes another type has no stages.

.. autosummary::
   :toctree: _api
   :nosignatures:

   results.RESULT_TYPES
   results.ResultType
   results.result_type
   results.model_result

A concentration result
----------------------

.. currentmodule:: runtraj.concentration

One release's result is an :class:`xarray.DataTree` with a child per grid,
named as the grid is in ``config.yaml``. Each grid is a Dataset over
:data:`CONCENTRATION_DIMS`:

.. list-table::
   :header-rows: 1
   :widths: 18 18 64

   * - Name
     - Units
     - Meaning
   * - ``time``
     - UTC
     - When each sample starts. In a backward run each sample starts
       before the one above it.
   * - ``time_end``
     - UTC
     - When each sample ends, a coordinate along ``time``. A snapshot
       starts and ends at once.
   * - ``z``
     - m
     - The top of each level above ground, in whole meters. ``0`` is the
       ground, where deposition is counted.
   * - ``lat``, ``lon``
     - degrees
     - The centers of the grid's cells.
   * - one variable per pollutant
     - set by the model
     - What the model counts, which its ``units`` and ``long_name``
       attributes say. HYSPLIT's concentration: mass m⁻³ in the unit of
       mass of the pollutant's emission rate, and at ``z = 0``, mass m⁻²
       deposited. With ``cmass: 1``: the mass in each cell, in the same
       unit of mass, and nothing at ``z = 0``. Zero where the model wrote
       nothing.

The file of one release is NetCDF 4, a group per grid, which
:func:`xarray.open_datatree` opens, as do ``ncdump`` and R's ``ncdf4``. Its
root attributes say what made it: ``release`` (the release id),
``runtraj:settings`` and ``runtraj:met_files`` (JSON, as a particle file's
metadata), ``runtraj:hash`` and ``runtraj:version``, and, in an ensemble,
``runtraj:realization``. Writing and reading it needs ``netCDF4``, which
``pip install 'runtraj[concentration]'`` installs.

.. autosummary::
   :toctree: _api
   :nosignatures:

   CONCENTRATION_DIMS
   check_concentration
   describe_grid
   read_concentration
   write_concentration
   require_netcdf4

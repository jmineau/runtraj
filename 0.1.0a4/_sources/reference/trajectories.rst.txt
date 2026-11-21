Trajectories
============

.. currentmodule:: runtraj.trajectories

A transport model in a trajectory mode, such as HYSPLIT's ``mode:
trajectory`` (:doc:`../guides/trajectories`), follows single air parcels
with the mean wind and no turbulence. Its result is the ``trajectories``
type (:doc:`concentration` lists the types): one table per release, a row
for each trajectory at each output time. ``sim.trajectories`` reads one
simulation's and :meth:`runtraj.Project.trajectories` many.

.. list-table::
   :header-rows: 1
   :widths: 18 18 64

   * - Column
     - Units
     - Meaning
   * - ``trajectory``
     -
     - The trajectory's number in its release, from 1: a point release
       starts one, and a multipoint release one from each point, in the
       order of the points. Stored as int32.
   * - ``time``
     - UTC
     - When the row is.
   * - ``age``
     - minutes
     - Time since the release, negative in a backward run. Each
       trajectory has a row at ``age = 0``, where it started. Stored as
       int32.
   * - ``lon``, ``lat``
     - degrees
     - Where the trajectory is.
   * - ``zagl``
     - m
     - Its height above ground.

A model adds a column for each value it writes along the trajectory.
HYSPLIT always writes ``pres``, and one more for each ``tm_`` setting that
is 1:

.. list-table::
   :header-rows: 1
   :widths: 14 16 16 54

   * - Column
     - Setting
     - Units
     - Meaning (HYSPLIT's label in its file)
   * - ``pres``
     - always
     - hPa
     - Pressure (``PRESSURE``).
   * - ``tpot``
     - ``tm_tpot``
     - K
     - Potential temperature (``THETA``). Also written when the vertical
       motion is isentropic (``w_option: 2``).
   * - ``temp``
     - ``tm_tamb``
     - K
     - Air temperature (``AIR_TEMP``).
   * - ``rain``
     - ``tm_rain``
     - mm per hour
     - Rainfall (``RAINFALL``).
   * - ``mlht``
     - ``tm_mixd``
     - m
     - Mixed-layer depth (``MIXDEPTH``).
   * - ``relh``
     - ``tm_relh``
     - percent
     - Relative humidity (``RELHUMID``).
   * - ``sphu``
     - ``tm_sphu``
     - g/kg
     - Specific humidity (``SPCHUMID``).
   * - ``mixr``
     - ``tm_mixr``
     - g/kg
     - Water vapor mixing ratio (``H2OMIXRA``).
   * - ``zsfc``
     - ``tm_terr``
     - m
     - Terrain height above sea level (``TERR_MSL``). ``zagl + zsfc`` is
       the trajectory's height above sea level.
   * - ``dswf``
     - ``tm_dswf``
     - W m⁻²
     - Downward shortwave flux (``SUN_FLUX``).

The values are HYSPLIT's, as its file holds them, to one decimal (three
for ``lon`` and ``lat``). A column that shares its name with a particle
column (:doc:`particles`) is the same quantity, but HYSPLIT's trajectory
program writes humidity in g/kg, so check a unit here before you compare
the two.

The table is not the particle table. It has no ``foot`` and no ``weight``,
the particle transforms do not run on it, and a stage that reads particles
is refused on a variant that makes trajectories.

The file of one release is Parquet, under ``trajectories/`` in the output
directory (:doc:`layout`), with the table's columns after a ``release``
column that holds the release id. Its metadata is a particle file's:
``runtraj:release``, ``runtraj:settings``, ``runtraj:met_files``,
``runtraj:hash`` and ``runtraj:version``.

.. autosummary::
   :toctree: _api
   :nosignatures:

   TRAJECTORY_SCHEMA
   check_trajectories
   read_trajectories
   write_trajectories
   trajectories_from_table

Trajectories
============

Use this page when you want the path of the air itself: one line from each
starting point, following the mean wind, with no turbulence. That is
HYSPLIT's trajectory program, ``hyts_std``, and runtraj runs it with
``mode: trajectory`` in the ``hysplit`` block. Each release gives a small
table, a row for each trajectory at each output time.

We recommend it for a first look at where air came from or went, and for
the many-year sets of trajectories that cluster and residence-time studies
use: a run follows one air parcel where a particle run follows thousands.
For the footprint of a
measurement or anything that depends on mixing, run particles instead
(:doc:`particles`): a single trajectory says nothing about how the air
spreads.

The examples on this page were run with NOAA's public HYSPLIT 5.4.2, and
their output is copied here. They need a HYSPLIT folder whose ``exec/`` has
``hyts_std``, which NOAA's download does.

One run
-------

A trajectory run needs only its length. Here one trajectory goes 6 hours
back from 10 m above Salt Lake Valley:

.. code-block:: python

   import runtraj

   release = runtraj.PointRelease(
       time="2021-07-15 10:30", longitude=-112.0, latitude=40.5, altitude=10
   )
   trajectories = runtraj.run_trajectories(
       release, "/data/hrrr", model="hysplit", n_hours=-6, mode="trajectory"
   )
   trajectories

.. code-block:: text

      trajectory                      time    age      lon     lat   zagl   pres
   0           1 2021-07-15 10:30:00+00:00      0 -112.000  40.500   10.0  853.1
   1           1 2021-07-15 09:30:00+00:00  -3600 -111.851  40.310    0.1  863.8
   2           1 2021-07-15 08:30:00+00:00  -7200 -111.865  40.049  280.5  834.0
   3           1 2021-07-15 07:30:00+00:00 -10800 -112.114  39.738  720.8  750.4
   4           1 2021-07-15 06:30:00+00:00 -14400 -112.340  39.480  250.0  831.0

``age`` is seconds since the release, negative going back, and the row at
``age = 0`` is the release itself. ``zagl`` is the height above ground in
meters, and ``pres`` the pressure in hPa, which HYSPLIT always writes.

This trajectory has 4 hours of rows, not 6: it left the met files' domain,
a small crop here, and a trajectory ends where it leaves. That is a
complete run, as it is for particles that all leave. Met files that end
before the run does are a failure (:doc:`checking`).

More rows and more columns
--------------------------

``tout`` is the time between rows, in minutes: 60 unless you set it. Under
an hour it must divide 60, because HYSPLIT quietly lowers any other value
to one that does (45 would give a row every 30 minutes), so runtraj
refuses it and names the values that work.

HYSPLIT can write nine more values along a trajectory, each asked for by
setting its ``tm_`` setting to 1:

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Setting
     - Column
     - Value
   * - ``tm_tpot``
     - ``tpot``
     - Potential temperature, K
   * - ``tm_tamb``
     - ``temp``
     - Air temperature, K
   * - ``tm_rain``
     - ``rain``
     - Rainfall, mm per hour
   * - ``tm_mixd``
     - ``mlht``
     - Mixed-layer depth, m
   * - ``tm_relh``
     - ``relh``
     - Relative humidity, percent
   * - ``tm_sphu``
     - ``sphu``
     - Specific humidity, g/kg
   * - ``tm_mixr``
     - ``mixr``
     - Water vapor mixing ratio, g/kg
   * - ``tm_terr``
     - ``zsfc``
     - Terrain height, m above sea level
   * - ``tm_dswf``
     - ``dswf``
     - Downward shortwave flux, W m⁻²

Heights are always above ground. For heights above sea level, set
``tm_terr: 1`` and add ``zsfc`` to ``zagl``.

Several trajectories from one release
-------------------------------------

A multipoint release starts one trajectory from each of its points, and
``trajectory`` numbers them in the order of the points. Two points may be
at one place, at different heights, which is how you compare levels:

.. code-block:: python

   levels = runtraj.MultiPointRelease(
       time="2021-07-15 02:00",
       longitudes=[-112.0, -112.0, -111.5],
       latitudes=[40.5, 40.5, 41.0],
       altitudes=[10, 500, 100],
   )
   trajectories = runtraj.run_trajectories(
       levels, "/data/hrrr", model="hysplit", n_hours=6, mode="trajectory", tout=30, tm_tpot=1, tm_mixd=1
   )
   trajectories.head(6)

.. code-block:: text

      trajectory                      time   age  ...   pres   tpot    mlht
   0           1 2021-07-15 02:00:00+00:00     0  ...  851.4  316.8   354.5
   1           2 2021-07-15 02:00:00+00:00     0  ...  805.6  318.6   354.5
   2           3 2021-07-15 02:00:00+00:00     0  ...  817.1  319.7  3653.8
   3           1 2021-07-15 02:30:00+00:00  1800  ...  856.7  315.8   386.2
   4           2 2021-07-15 02:30:00+00:00  1800  ...  813.8  318.0   486.9
   5           3 2021-07-15 02:30:00+00:00  1800  ...  796.8  319.7  3253.2

How high did each get?

.. code-block:: python

   trajectories.groupby("trajectory")["zagl"].max()

.. code-block:: text

   trajectory
   1      43.3
   2    1047.2
   3    1030.6
   Name: zagl, dtype: float64

A column release is refused in this mode. A column is a line of particles
spread over its height, and a trajectory starts from a point: give the
heights you want as points.

In a project
------------

In ``config.yaml`` the mode is a setting of the ``hysplit`` block, and a
variant may set it, so one project can hold particles and trajectories of
the same releases:

.. code-block:: yaml

   models:
     hysplit:
       n_hours: -72
       numpar: 1000
   variants:
     back: {}                        # particles
     traj:
       mode: trajectory
       tm_tpot: 1
       tm_rain: 1

The ``traj`` variant takes ``n_hours`` from the block and leaves the
particle settings, such as ``numpar``, out of its run and its record: the
trajectory program does not read them. Run the project as any other, and
read each type with its own verb:

.. code-block:: python

   project = runtraj.Project("my_project")
   project.run()
   project.trajectories()                          # every trajectory, with release and variant
   project.simulation(release_id, "traj").trajectories
   project.particles()                             # the particles of the back variant

``project.status()`` gets a ``trajectories`` column, and the files are in
``trajectories/`` of the output directory, one Parquet file per release
(:doc:`../reference/layout`). A trajectory is told from another by
``release``, ``variant`` and ``trajectory`` together.

What this mode does not do
--------------------------

- **Realizations.** Nothing in a trajectory run is random, so
  ``realizations`` above 1 would repeat one run, and is refused. HYSPLIT's
  trajectory ensemble (``hyts_ens``) is not run.
- **Stages and transforms.** They read particles. A project with a stage
  sets it to null in a trajectory variant (``footprint: null``).
- **Restarts and splits** (HYSPLIT's ``NSTR`` and ``NVER``). Add releases
  to start trajectories at more times.

The columns and units are in :doc:`../reference/trajectories`, and the
lines of HYSPLIT's input files in :ref:`hysplit-trajectory`.

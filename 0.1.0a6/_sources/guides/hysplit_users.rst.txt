runtraj For HYSPLIT Users
=========================

If you run HYSPLIT to find where air came from or where a release went,
runtraj runs the same particle model, ``hycs_std``, from Python. It writes
``CONTROL`` and ``SETUP.CFG`` from a few settings, finds or downloads the
ARL met files, and returns every particle's position at every time step as
a pandas table. For many starting times and places, and for the same runs
under other settings, a project runs them all, on your computer or as a
Slurm job array, and keeps the particles of each set of settings in a
folder of its own. It runs the ``hycs_std`` you already have: set
``directory`` to the HYSPLIT folder, or put the folder of ``hycs_std`` on ``PATH``
(:doc:`../getting_started/installation`).

.. ipython:: python
   :suppress:

   import docs_examples

   met_dir = docs_examples.met()

HYSPLIT's words in runtraj
--------------------------

runtraj calls the time and place particles start from a *release*. For
you it is the starting time and starting locations of ``CONTROL``:

.. list-table::
   :header-rows: 1
   :widths: 45 55

   * - In HYSPLIT
     - In runtraj
   * - A starting time and one starting location
     - A :class:`~runtraj.PointRelease`
   * - Several starting locations at one time
     - A :class:`~runtraj.MultiPointRelease`
   * - Two heights at one location, a line source
     - A :class:`~runtraj.ColumnRelease`
   * - ``KMSL``, heights above ground or above sea level
     - The release's ``altitude_ref``, ``"agl"`` or ``"msl"``
   * - The settings of one ``CONTROL`` and ``SETUP.CFG``
     - A variant, a named set of settings
   * - The particle positions you read after a run
     - The particle table, one row per particle per time step

One run
-------

:func:`runtraj.run_trajectories` runs HYSPLIT once and returns the particles.
Here 200 particles leave Salt Lake City at 500 m above ground during one
hour and are followed 6 hours forward:

.. ipython:: python

   import runtraj

   start = runtraj.PointRelease(
       time="2021-07-15 01:00", longitude=-111.85, latitude=40.77, altitude=500
   )
   cloud = runtraj.run_trajectories(
       start, met_dir, model="hysplit", n_hours=6, numpar=200, emisshrs=1.0,
       krand=2, seed=42,
   )
   cloud[["time", "particle", "lon", "lat", "zagl"]].tail()

``n_hours`` is ``CONTROL``'s total run time: positive runs forward, negative
backward. ``emisshrs`` is the hours of emission, and ``zagl`` the height
above ground in meters. The other columns are the met at each particle that
``varsiwant`` asks for, by HYSPLIT's names (``mlht``, ``pres``, ``dens``,
...), and ``foot`` is HYSPLIT's own. ``krand=2`` with a ``seed`` makes the
run repeatable.

The met is the folder of your ARL files. runtraj reads which times each
file holds from its header, and gives HYSPLIT the ones the run needs.
:doc:`meteorology` shows how to download them from NOAA instead.

.. _single-trajectory:

A single trajectory
-------------------

``hyts_std``, HYSPLIT's trajectory model, follows the mean wind. One
particle with turbulence off follows the same path, so ``numpar=1`` and
``nturb=1`` give you a trajectory:

.. ipython:: python

   path = runtraj.run_trajectories(
       start, met_dir, model="hysplit", n_hours=6, numpar=1, nturb=1
   )
   path[["time", "lon", "lat", "zagl"]].iloc[[0, 59, 179, 359]]

We compared this run with ``hyts_std`` (NOAA's public 5.4.2) on the same
met. After 6 hours the two are 0.001 degrees apart in longitude and 0.008 in
latitude. The mean position of 200 particles with turbulence on ended about
41 km away and 340 m lower:

.. list-table::
   :header-rows: 1
   :widths: 46 18 18 18

   * - After 6 hours
     - Longitude
     - Latitude
     - Height (m)
   * - ``hyts_std``
     - -110.709
     - 40.544
     - 1354
   * - One particle, turbulence off
     - -110.707
     - 40.536
     - 1428
   * - Mean of 200 particles
     - -111.111
     - 40.735
     - 1087

So the mean of many particles is the center of a cloud that spreads as it
goes, which is a different thing from a trajectory. For a trajectory, run one
particle.
For several trajectories from one time, use a
:class:`~runtraj.MultiPointRelease` with ``numpar`` equal to its number of
locations: HYSPLIT gives each location one particle.

Many runs: a project
--------------------

A project runs every starting point under every variant, and runs again
only what is missing. Here every start runs on HRRR with two vertical motion
options, each kept in its own folder:

.. code-block:: yaml

   mets:
     hrrr: /data/arl/hrrr

   models:
     hysplit:
       n_hours: 24
       numpar: 1000
       emisshrs: 1.0

   variants:
     hrrr: {}                 # the settings above
     hrrr-w6: {w_option: 6}   # the same runs with another vertical motion

The starting points go in ``releases.csv``, one row each:

.. code-block:: text

   time,longitude,latitude,altitude
   2021-07-15 01:00:00,-111.85,40.77,500
   2021-07-15 13:00:00,-111.85,40.77,500

``runtraj run ./my_project`` runs them here, and ``runtraj submit
./my_project --backend slurm`` as a Slurm job array (:doc:`slurm`). Then read the particles
of any selection of runs as one table:

.. code-block:: python

   project = runtraj.Project("my_project")
   sims = project.simulations
   particles = project.particles(sims[sims.variant == "hrrr-w6"])

For an ensemble of the same run with different turbulence, give a variant
``realizations: 10`` with ``krand: 2`` and a ``seed``.

Where each ``CONTROL`` and ``SETUP.CFG`` line comes from
--------------------------------------------------------

Every ``SETUP.CFG`` entry but ``IDSP`` is a setting of the same name in
lowercase, such as ``numpar``, ``kmixd`` or ``tlfrac``
(:doc:`../reference/configuration` lists them). Each defaults to HYSPLIT's
own value with ``ICHEM=8``. A setting HYSPLIT decides only after reading the
file (``capemin``, ``cpack``, ``kblt``, ``kmixd``, ``krand``,
``vscales``) is left out unless you give it, so HYSPLIT chooses. Some act
only with another: the puff settings when ``initd`` releases puffs, and
``pinpf`` when ``ninit`` reads particles from a file. ``delt`` has no
effect, since HYSPLIT steps 1 minute with ``ICHEM=8``. runtraj writes the
rest itself:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Line
     - Comes from
   * - ``CONTROL`` starting time and locations
     - The release (receptor), latitude, longitude and height
   * - ``CONTROL`` total run time
     - ``n_hours``
   * - ``CONTROL`` vertical motion option, top of model domain
     - ``w_option``, ``z_top``
   * - ``CONTROL`` met grids and files
     - The met's files for the run's hours, as one grid
   * - ``CONTROL`` hours of emission
     - ``emisshrs``; the release starts at the starting time
   * - ``CONTROL`` pollutant, concentration grid and deposition
     - Fixed: one pollutant, a placeholder grid, no deposition; with
       ``mode: concentration``, your ``pollutants`` and ``grids``
       (:doc:`concentration`); with ``mode: trajectory``, the two lines
       ``hyts_std`` reads in their place (:doc:`trajectories`)
   * - ``SETUP.CFG`` ``KMSL``
     - The release's ``altitude_ref``
   * - ``SETUP.CFG`` ``MAXPAR``
     - ``maxpar``, or when unset enough for ``numpar`` shared over the locations
   * - ``SETUP.CFG`` ``IVMAX``
     - The number of ``varsiwant`` names
   * - ``SETUP.CFG`` ``SEED``
     - ``seed``, written as ``-(seed + 1)``
   * - ``SETUP.CFG`` ``WINDERRTF``, ``ZICONTROLTF``
     - The wind and mixed-layer error settings, and ``ziscale``; with
       ``ziscale`` on HYSPLIT 5.3.0 and later, ``WINDERRTF`` is 2 less,
       because ``hycs_std`` adds 2 itself once it has read ``ZICONTROL``
   * - ``SETUP.CFG`` ``ICHEM``, ``IDSP``
     - 8 and 2, the only values with which HYSPLIT writes particles
       (:data:`~runtraj.models.hysplit.config.FIXED_SETUP`; ``ichem`` may
       be 8 or left out); with ``mode: concentration``, ``ICHEM`` from
       ``ichem``, 0 unless set, and ``IDSP`` left to HYSPLIT
       (:doc:`concentration`)
   * - ``CHEMRATE.TXT``
     - ``conversions``, with ``mode: concentration`` and ``ichem: 2``
   * - ``SETUP.CFG`` ``KAGL``, ``TOUT``, ``TM_*``
     - With ``mode: trajectory``: ``KAGL`` fixed at 1 (heights above
       ground), ``tout``, and the ``tm_`` settings

To read the files of one run, pass ``workdir="run"`` to
:func:`~runtraj.run_trajectories`: the folder keeps ``CONTROL``,
``SETUP.CFG``, HYSPLIT's ``MESSAGE`` and the particle file.

What runtraj leaves to other tools
----------------------------------

runtraj runs ``hycs_std`` with ``ICHEM=8``, which writes
particle positions and the met along them, or, with ``mode:
concentration``, HYSPLIT's concentration grids (``cdump``), which it reads
for you (:doc:`concentration`). With ``mode: trajectory`` it runs
``hyts_std`` and reads its ``tdump`` trajectory file
(:doc:`trajectories`). The ensemble programs and the particle dump file
(``PARDUMP``) come from HYSPLIT run on its own, and NOAA's
`monetio <https://github.com/noaa-oar-arl/monetio>`_ reads those files in
Python.

When a run fails, ``project.status()`` gives its reason and
``sim.log`` HYSPLIT's log of it.

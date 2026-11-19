Glossary
========

.. glossary::
   :sorted:

   release
   receptor
      Where and when particles start, in either direction: at the receptor
      in a backward run, at the source in a forward run (see the
      :ref:`diagram <release-source-receptor>`). A release placed at a
      measurement and followed backward in time is often called a receptor.
      A :class:`~runtraj.PointRelease` is one point (a tower inlet), a
      :class:`~runtraj.ColumnRelease` is a vertical column, and a
      :class:`~runtraj.MultiPointRelease` is a set of points, such as a
      slanted line of sight. See :doc:`../guides/releases`.

   particle
      An imaginary parcel of air that starts at a release and moves with the
      winds, plus random turbulence: backward in time in a backward run, and
      forward in time in a forward run. A run releases many
      (:term:`numpar`), which together sample where the air could have come
      from or gone.

   particles
      The table of every particle's position at every output step of one
      simulation: a pandas DataFrame in Python, a Parquet file on disk. See
      :doc:`../guides/particles`.

   trajectory
      The path of one particle through time: the rows of the
      :term:`particles` with one particle number.

   transport model
      The program that moves the particles through the meteorology.
      runtraj runs it; :term:`HYSPLIT` is the one built in, and another can
      be added by the import path of its class.

   HYSPLIT
      NOAA's particle dispersion model, written in Fortran, and runtraj's
      built-in :term:`transport model`. runtraj runs its ``hycs_std``
      executable and does not include it (:doc:`installation`).

   meteorology
      Gridded weather-model fields (winds, temperature, turbulence, mixed
      layer depth) that move the particles. Often shortened to *met*. For
      HYSPLIT it must be in :term:`ARL` format. See
      :doc:`../guides/meteorology`.

   ARL
      The file format for meteorology that HYSPLIT reads, named after
      NOAA's Air Resources Laboratory. NOAA publishes several weather
      models in this format, such as :term:`HRRR`, NAM (12 km, North
      America) and GDAS (global); :doc:`../guides/meteorology` has the
      table.

   HRRR
      The High-Resolution Rapid Refresh, NOAA's 3 km weather model of the
      contiguous United States, archived in :term:`ARL` format since 2015.
      The examples in these docs use it, and name their met ``hrrr``.

   met
      Short for :term:`meteorology`, and also one named set of meteorology
      files in your settings, such as
      ``hrrr``, written under ``mets`` in ``config.yaml``. Each
      :term:`variant` names the met it runs on, so the same release can run
      with several mets side by side.

   defaults
      In a project, the settings written in the blocks of ``config.yaml``:
      the transport model's under ``models`` and each stage's under
      ``stages``. Every :term:`variant` starts from them and overrides what
      it changes. A setting written in neither takes the transport model's
      own default (:doc:`../guides/choosing_settings`).

   variant
      A named set of settings that every release runs under: a :term:`met`
      plus any settings that differ from the :term:`defaults`, such as
      another :term:`ziscale` or more particles. See :doc:`../guides/projects`.

   realization
      One member of a variant declared with ``realizations: N``. The same
      settings run ``N`` times, as realizations ``0`` to ``N - 1``, each
      with its own random numbers (``seed + k`` with ``krand: 2``). Their results share the
      variant's folder, a ``realization=k`` partition each.

   stage
      What is made from a simulation's particles after the transport model
      runs, one file per simulation, by a class in another package that
      ``config.yaml`` names. PYSTILT's footprint is an example. See
      :doc:`../guides/projects`.

   simulation
      One release run under one :term:`variant` (and, in an ensemble, one
      :term:`realization`). A project with 100 releases and 3 variants has 300
      simulations.

   release id
      The name of a release and of its result files, such as
      ``202101150800_-111.848_40.766_10``: the release time as
      ``YYYYMMDDHHMM`` in UTC, then the longitude, latitude, and altitude.
      Column releases end in ``X`` and the bottom and top (``_X0-3000``)
      instead of an altitude. Multipoint releases use ``multi_`` and a short
      hash instead of coordinates. Heights above mean sea level add ``msl``
      at the end.

   project
      A folder holding your settings (``config.yaml``), your releases
      (``releases.csv``), and any other inputs a stage reads (``tables/``).
      The results go to the :term:`output directory` it names. See
      :doc:`../guides/projects`.

   output directory
      Where a project's results go, named by ``output:`` in ``config.yaml``
      (``output`` in the project folder by default, or a URL such as
      ``s3://bucket/output``). It holds one :term:`settings folder` per set
      of settings under ``particles/`` and each stage's tree, and
      ``logs/``. Several projects can share it. See
      :doc:`../guides/output`.

   settings folder
      One folder of results in the :term:`output directory`: everything made
      with one set of settings, named after the variant that first made it
      and six characters computed from the settings, such as
      ``settings=hrrr-2e01ed``. A changed setting makes a new folder and
      never overwrites one, and projects with the same settings share a
      folder. ``runtraj status`` lists them.

   workdir
      The folder one simulation's transport model runs in, new for each
      run. It is removed after a successful run and kept under
      ``scratch/`` in the output directory when the run fails
      (``sim.kept_workdir``).

   compute root
      The folder the workdirs are made in, usually fast local disk: the
      ``--compute-root`` option, else ``RUNTRAJ_COMPUTE_ROOT``, else
      ``$TMPDIR/runtraj/<project>``. Projects can share one.

   mixed layer
      The layer next to the ground that turbulence keeps mixed, from about
      100 m deep at night to 1 to 2 km on a summer afternoon. Particles in
      it feel the surface. Its depth is often called the boundary-layer
      height (PBLH in a weather model); HYSPLIT computes its own by
      default (:doc:`../guides/choosing_settings`).

   ziscale
      A HYSPLIT setting that multiplies the :term:`mixed layer` depth:
      ``ziscale: 0.8`` makes it 20% shallower. A common sensitivity
      variant.

   varsiwant
      The HYSPLIT setting that lists the met variables written beside each
      particle position, such as ``mlht``, the mixed-layer height. They
      become columns of the :term:`particles`
      (:doc:`../reference/particles`).

   Parquet
      A column-based file format for tables, which pandas, pyarrow, polars,
      DuckDB and R's ``arrow`` read. Each simulation's particles are one
      Parquet file.

   Slurm
      The job scheduler of many HPC clusters. runtraj can submit a project
      as a Slurm job array, one task per share of the releases
      (:doc:`../guides/slurm`).

   backend
      Where the simulations run: ``local`` (your computer) or ``slurm`` (an
      HPC cluster). Set it in the ``execution`` block of ``config.yaml``,
      with that backend's settings in the block of its name. See
      :doc:`../guides/running`.

   numpar
      The number of particles released per simulation, 2500 unless set.
      More particles sample the turbulence better and take longer. We use
      1000 for a tower.

   n_hours
      How many hours to follow the particles. Negative values run backward
      in time, the usual case for a measurement. ``-24`` means one day
      back.

   AGL
      Above ground level. Release altitudes are AGL unless the release sets
      ``altitude_ref="msl"``.

   MSL
      Above mean sea level.

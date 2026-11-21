Output Layout
=============

This page describes the files in an output directory, so that a reader in
R, Julia, DuckDB, or plain pyarrow can read them without runtraj. In Python,
:func:`runtraj.read_particles` and :meth:`runtraj.Project.particles` do all
of this for you.

The folders
-----------

Each kind of result has its own tree: ``particles/`` for what the transport
model wrote (``concentration/`` for a variant that makes concentration,
:doc:`concentration`), and one named for each stage of the project, which is what is
made from a simulation's results afterward. PYSTILT's footprint is a stage,
and its tree is ``footprint/``; the examples here show it. Inside a tree,
each settings folder holds the
results of one set of settings, split by the release's date:

.. code-block:: text

   <output>/
     particles/
       settings=hrrr-7d36c8/
         _settings.yaml
         date=2024-07-15/<release id>.parquet
       settings=hrrr-err-b2399e/                 an ensemble
         _settings.yaml
         realization=0/date=2024-07-15/<release id>.parquet
         realization=1/date=2024-07-15/<release id>.parquet
     concentration/                              a variant with mode: concentration
       settings=conc-41e9a0/
         _settings.yaml
         date=2024-07-15/<release id>.nc
     footprint/
       settings=hrrr-f82d40/
         _settings.yaml
         date=2024-07-15/<release id>.parquet
     logs/
       settings=hrrr-7d36c8/date=2024-07-15/<release id>.log
       settings=hrrr-7d36c8/date=2024-07-15/<release id>.failure.yaml
       settings=hrrr-f82d40/date=2024-07-15/<release id>.failure.yaml
     scratch/
       settings=hrrr-7d36c8/date=2024-07-15/<release id>/

A settings folder is named ``<variant>-<six characters>``. The variant is
the name in ``config.yaml`` that first made the folder, and the six
characters are the start of its settings hash (below). The date is the
release's release date in UTC. An ensemble (``realizations: N``) has a
``realization=k`` folder between the settings folder and the dates.

The ``logs/`` tree follows the folders of the results:

- ``<release id>.log`` is the transport model's log, under the particles'
  settings folder (the concentration's, for a variant that makes it).
- ``<release id>.failure.yaml`` says why a result failed, and at which
  ``stage``. A failed run writes it under the particles' settings folder,
  with ``stage: particles``. A failed footprint writes it under the
  footprint's own settings folder, with ``stage: footprint``, since the
  particles were fine. It is removed when the result is written.

``scratch/`` holds kept workdirs, one folder per simulation, under the
particles' settings folder. A failed run's workdir is kept without the
transport model's particle file, and with ``keep_workdir: true`` under
``execution:`` every run's is, whole.

The ``particles/`` and ``footprint/`` trees hold only Parquet files and
``_settings.yaml``, so each reads as one dataset. ``concentration/`` holds
one NetCDF file per release, a group per grid (:doc:`concentration`). A stage from another
package has a tree under its own name, with files in the format and with
the extension that stage chooses.

A simulation is complete when the model's output file exists, and so does
the file of every stage its variant uses: for a variant with a footprint,
the particle file and the footprint file. A stage writes a file even when
it has nothing to put in it, as an empty footprint is, so that the
simulation is complete.

``_settings.yaml``
------------------

Every settings folder holds a ``_settings.yaml``. A particles folder's:

.. code-block:: yaml

   name: hrrr
   hash: 7d36c80b3b5a1a33cc355998f5755de469f61c53151a9d1b953fa698c81cd51f
   runtraj: 0.1.0a23
   model_version: hysplit.v5.1.0
   settings:
     capemin: -1
     # ... every transport parameter, in name order
     met:
       crop: null
       source: HRRR
     model:
       data_files:
         ASCDATA.CFG: fd489592654bf600fd96b08d2ccde76f90f0672b7e872fb9fa33dd363fbaa643
         LANDUSE.ASC: de63b2e3f405cdecbe2f4599ba276bf21b5bb05d786cd54e9c24a051c42b8511
         ROUGLEN.ASC: a8503953877b09e906e85364e4081c43ffb5d7353e3ffdb259094acbb95588b1
         TERRAIN.ASC: 0608b518d39ca63fe8774aef887f71f451b1c698c4b708bead36eb926ab86268
       name: hysplit
       sha256: 37346bee81a62f31d2f273c9813d53a76c54d59cfd5adbf3f76a7f870e3f49f9
     n_hours: -24
     # ...

A stage's folder, such as a footprint's, names its stage and what its files
were made from:

.. code-block:: yaml

   name: hrrr
   hash: f82d40aea8c29b2a30b99b1c5a74166937ee4eef5df20aa80407bfb69a3ab7a0
   stage: footprint
   class: stilt.footprint.FootprintStage
   reads:
     particles:
       folder: hrrr-7d36c8
       hash: 7d36c80b3b5a1a33cc355998f5755de469f61c53151a9d1b953fa698c81cd51f
   versions:
     runtraj: 0.1.0a30
   settings:
     cells_per_target: 4
     geometry: null
     geometry_hash: null
     grid:
       crs: +proj=longlat
       xmin: -112.5
       xmax: -111.5
       xres: 0.01
       ymin: 40.25
       ymax: 41.25
       yres: 0.01
     smooth_factor: 1
     transforms: []
     tres: 1h

``name``
   The variant that made the folder.
``hash``
   The folder's settings hash, a SHA-256 in hex. The folder name ends in
   its first six characters.
``runtraj``
   In a particles folder, the runtraj version that wrote the folder.
``model_version``
   In a particles folder only: the version the transport model's build
   reports, such as what ``hycs_std`` prints when it starts. It is not in
   the hash: ``model.sha256`` is.
``settings``
   What the results were made with. In a particles folder, the transport
   model's parameters, ``met`` (the weather product and its crop), and
   ``model`` (the transport model and its build: the SHA-256 of
   ``hycs_std``, and of each of HYSPLIT's data tables). An ensemble adds
   ``ensemble: true``, and realization ``k`` ran with ``seed + k``. In a
   stage's folder, the stage's own settings: for a footprint, the
   footprint settings, with the grid.
``stage``, ``class``
   In a stage's folder only: the stage's name, which is also its tree, and
   the import path of its class, as ``config.yaml`` names it.
``reads``
   In a stage's folder only: what its files were made from, each with the
   name and the hash of its settings folder. ``particles`` is always there;
   a stage that reads another stage's files lists that stage too.
``versions``
   In a stage's folder only: the runtraj version that wrote the folder,
   and the version of the stage's own package when it is another one.

A particles folder's hash covers its ``settings`` block. A stage folder's
hash covers its ``settings`` block together with the hash of each thing
under ``reads``, so the same footprint settings on other particles give
another folder. The versions are not part of a hash. Every particle and
footprint file records its folder's full hash as ``runtraj:hash``.

Particle files
--------------

A particle file is ``particles/settings=.../date=.../<release id>.parquet``,
one row per particle per output step. :doc:`particles` describes what each
column means.

.. list-table::
   :header-rows: 1
   :widths: 20 25 55

   * - Column
     - Stored as
     - Notes
   * - ``release``
     - dictionary of string
     - The release id, the same on every row.
   * - ``particle``
     - int32
     - :func:`runtraj.read_particles` gives it back as float64.
   * - ``age``
     - int32
     - Minutes since release, negative for a backward run. Read back as
       float64.
   * - ``lon``, ``lat``, ``zagl``, and the rest
     - float64
     - The columns ``varsiwant`` asked HYSPLIT for, plus ``release_height``
       for a column or multipoint release.

``time`` is not stored: it is the release time plus ``age``.

The file's metadata (Parquet key-value metadata):

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Key
     - Value
   * - ``runtraj:release``
     - The release as JSON (below).
   * - ``runtraj:settings``
     - The run's settings as JSON, the ``settings`` block of the folder's
       ``_settings.yaml``.
   * - ``runtraj:met_files``
     - The met files the run read, a JSON list of paths.
   * - ``runtraj:hash``
     - The folder's settings hash.
   * - ``runtraj:version``
     - The runtraj version that wrote the file.
   * - ``runtraj:realization``
     - The realization number, in an ensemble only.

Release ids and the release
-----------------------------

A release id is the release time to the minute, then the location:

.. code-block:: text

   202407151830_-111.85_40.77_5            a point: lon_lat_altitude
   202407151830_-111.85_40.77_X0-3000msl   a column: lon_lat_X<bottom>-<top>
   202407151830_multi_7bb3ffdc53           a multipoint: a hash of its points

A location ends in ``msl`` when its heights are above sea level. Whole
numbers are written without a decimal point.

``runtraj:release`` holds the whole release as JSON. Its ``time`` keeps
the seconds, and is UTC with no time zone:

.. code-block:: text

   {"kind": "point", "time": "2024-07-15T18:30:00", "altitude_ref": "agl",
    "longitude": -111.85, "latitude": 40.77, "altitude": 5.0}

   {"kind": "column", "time": "2024-07-15T18:30:00", "altitude_ref": "msl",
    "longitude": -111.85, "latitude": 40.77, "bottom": 0.0, "top": 3000.0}

   {"kind": "multipoint", "time": "2024-07-15T18:30:00", "altitude_ref": "agl",
    "longitudes": [-111.85, -111.86], "latitudes": [40.77, 40.78],
    "altitudes": [5.0, 10.0]}

Reading many files
------------------

Read as a dataset with hive partitioning, a tree gives the folder names as
columns: ``settings``, ``date``, and in an ensemble ``realization``.
DuckDB (``hive_partitioning = true``), polars, R's arrow, and
``pyarrow.dataset`` all do this. A reader of one file gets only the stored
columns.

Keys you do not know
--------------------

A reader should skip metadata keys and ``_settings.yaml`` keys it does not
know. Later versions may add some, and files rewritten from development
builds may still carry old ones.

Which rules a file follows
--------------------------

A file follows the rules of the runtraj version that wrote it:
``runtraj:version`` in a result file, and ``runtraj`` in a
``_settings.yaml`` (under ``versions`` in a stage's folder). There is no separate format number. This page
describes the files of the current version.

Why the output has separate trees
---------------------------------

The output directory keeps results, logs, and kept workdirs in separate
trees (``particles/``, ``footprint/``, ``logs/``, ``scratch/``). Each
results tree holds result files only, so pyarrow, DuckDB, polars, and R
read ``particles/`` or ``footprint/`` as one table, and a log or a failure
record beside a result would break that. Whether a simulation is complete is read from the results
trees alone; the logs tree only explains the ones that are not.

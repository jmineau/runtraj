Projects And Variants
=====================

A :term:`project` is a folder with your settings and your releases. Every
release runs once under each :term:`variant`, a named set of settings, and
the results go to an output directory the settings name. We keep one
project per study: a project reruns only what is missing and never
overwrites a result, so you can add releases and try other settings as the
study grows. Most projects need only the settings below, and
:doc:`../reference/configuration` lists every option.

The project folder
------------------

.. code-block:: text

   my_project/
     config.yaml        # your settings: meteorology, transport model, variants
     releases.csv       # your releases: where and when particles start
     tables/            # other inputs a stage reads (project.add_table)

:meth:`Project.init <runtraj.Project.init>` makes the folder and writes the
settings you pass it to ``config.yaml``, once. It stops with a
``FileExistsError`` if the folder already has a ``config.yaml``. You can also
write both files by hand (:doc:`../getting_started/quickstart` shows how).

Both files are yours to edit. runtraj never rewrites ``config.yaml`` and
only appends to ``releases.csv``. ``project.add_table("name", table)``
writes ``tables/name.parquet`` for a stage that reads a table. A Slurm run
also makes a ``_slurm/`` folder with its job scripts and logs
(:doc:`slurm`).

A typical config.yaml
---------------------

.. code-block:: yaml

   mets:
     hrrr: /data/arl/hrrr

   models:
     hysplit:
       n_hours: -24
       numpar: 1000

   variants:
     hrrr: {}            # every release runs once, with these settings

The file is made of blocks, and no setting sits outside one:

``mets``
   Your meteorology, by name (:doc:`meteorology`).
``models``
   The transport model and its settings. ``hysplit`` is the one built in;
   left out, the project runs HYSPLIT with its own defaults.
``variants``
   What runs (`Variants`_).
``stages``
   What is made from each simulation's particles after the run, if
   anything (`Stages`_). Without it, a project makes particles only.
``execution``
   Where to run. Leave it out to run on your own computer (:doc:`running`).
``output``
   Where results go, ``output`` inside the project folder by default
   (`The output directory`_).

A setting written at the top of the file is refused with a message that
names the block it belongs in. A key runtraj does not know is an error that
names the nearest setting, so a typo such as ``numpr`` is caught when the
file loads, before anything runs.

The settings most people change
-------------------------------

.. list-table::
   :header-rows: 1
   :widths: 22 53 25

   * - Setting
     - What it does
     - Typical value
   * - ``n_hours``, under ``models: hysplit:``
     - How many hours to follow the particles. Negative runs backward in
       time, from a measurement toward its sources. Positive runs forward,
       from a source toward where the air goes. It has no default.
     - ``-24`` to ``-72``
   * - ``numpar``, under ``models: hysplit:``
     - Particles released per simulation. More particles sample the
       turbulence better and take longer.
     - ``1000``
   * - ``krand`` and ``seed``, under ``models: hysplit:``
     - ``krand: 2`` with a ``seed`` gives the same particles on every run,
       so a rerun or a colleague's run matches yours. Without them HYSPLIT
       draws its random numbers from the clock.
     - ``2`` and ``42``

:doc:`choosing_settings` says how we choose ``n_hours`` and ``numpar`` for a
study. Every other HYSPLIT setting, such as turbulence or the time step,
takes HYSPLIT's own default, and most projects never change them.
:doc:`hysplit_users` maps each to HYSPLIT's ``CONTROL`` and ``SETUP.CFG``.

Variants
--------

Every release runs once per variant. A variant has a name, a met, and any
settings that differ from the blocks above, which are its defaults. Only the
variants listed run, and an entry with no overrides (``hrrr: {}``) runs the
defaults unchanged. Add entries to run the same releases under other
settings:

.. code-block:: yaml

   variants:
     hrrr: {}                     # the defaults with the hrrr met
     hrrr-zi08: {ziscale: 0.8}    # a mixed-layer sensitivity
     hrrr-np3k: {numpar: 3000}    # a particle-count check

One release under one variant is a :term:`simulation`, so 100 releases and
three variants make 300 simulations. A variant may set:

``met``
   Which met to use. A variant named after a met uses that met, so you
   need ``met`` only when ``mets`` has more than one entry and the variant
   has another name.

``realizations``
   Run the variant ``N`` times, as realizations ``0`` to ``N - 1`` of one
   ensemble. Each realization needs its own random numbers, so set
   ``krand: 2`` and a ``seed``: realization ``k`` runs with ``seed + k``.
   Raising ``N`` later only adds realizations.

``model``
   Which block under ``models`` it runs. You need it only when ``models``
   has more than one entry.

``stages``
   The settings of a stage this variant changes, under the stage's name,
   or null for a variant without that stage (`Stages`_).

Any other key
   A setting of the variant's transport model (``numpar``, ``ziscale``,
   ``n_hours``), which replaces the model block's value for this variant
   only.

Variant names may use lowercase letters, digits, and hyphens, and must
start with a letter or digit, because they become folder names. Met names
follow the same rule.

Stages
------

A stage makes one file per simulation from its particles, after the
transport model runs. runtraj has none built in: a stage comes from another
package, and ``config.yaml`` names its class by import path, in a block
named for the stage. PYSTILT's footprint is an example:

.. code-block:: yaml

   stages:
     footprint:
       class: stilt.footprint.FootprintStage
       grid: {xmin: -113.0, xmax: -110.5, ymin: 40.0, ymax: 42.0, xres: 0.01, yres: 0.01}

   variants:
     hrrr: {}
     hrrr-coarse: {stages: {footprint: {grid: {xres: 0.05, yres: 0.05}}}}
     hrrr-particles: {stages: {footprint: null}}   # particles only

Every setting after ``class`` belongs to the stage; its own documentation
lists them. A stage's results go to a tree of the output directory named
for its block (``footprint/`` here), and ``project.status()`` gets a column
of that name.

Running the transport model is the slow part of a simulation; a stage
usually takes seconds. Variants that differ only in their stage settings,
such as ``hrrr`` and ``hrrr-coarse`` above, have the same transport
settings, so the model runs once per release for both, and each makes its
own stage file from the shared particles. There is nothing to declare:
runtraj sees that the settings that decide the particles are equal. Add
such a variant to a finished project and run again, and only the new stage
files are made.

The output directory
--------------------

``output:`` in ``config.yaml`` says where results go. Any path works, and
projects that name the same directory share it and the results in it:

.. code-block:: text

   output/
     particles/
       settings=hrrr-a3f9c2/                 # one folder per set of transport settings
         _settings.yaml                      # the settings, written out in full
         date=2021-01-15/<release id>.parquet
     logs/
       settings=hrrr-a3f9c2/date=2021-01-15/<release id>.log
       settings=hrrr-a3f9c2/date=2021-01-15/<release id>.failure.yaml   # a failed run
     scratch/
       settings=hrrr-a3f9c2/date=2021-01-15/<release id>/              # a kept workdir

A project with a stage also has a tree named for it, beside
``particles/``. Each :term:`settings folder` is named after the variant that
first made it, plus six characters computed from the settings it was made
with. So a changed setting never overwrites a result: edit ``ziscale`` and
the next run writes into a new folder beside the old one, and projects with
the same settings share a folder. The ``_settings.yaml`` in each folder
lists the settings in full, so a folder explains itself. The logs and the
kept workdirs are explained in :doc:`checking`.

The folder names are what pyarrow, DuckDB, polars, and R's ``arrow`` read as
columns, so ``output/particles`` opens as one table with ``settings`` and
``date`` columns, no runtraj needed (:doc:`particles`).
:doc:`../reference/layout` describes every file, for a reader written
without runtraj.

On an object store
~~~~~~~~~~~~~~~~~~

The output directory can be on an object store such as Amazon S3 or Google
Cloud Storage. Write it as a URL:

.. code-block:: yaml

   output: s3://my-bucket/slv-2021

runtraj reads and writes it through `fsspec
<https://filesystem-spec.readthedocs.io>`_, so install the package for your
store: ``pip install s3fs`` for ``s3://``, ``gcsfs`` for ``gs://``.
Credentials come from where those packages look for them, such as
``~/.aws/credentials`` or ``AWS_ACCESS_KEY_ID``. For another store that
speaks the S3 protocol, set ``AWS_ENDPOINT_URL``.

Everything else works the same: the folder layout, reruns that skip
finished simulations, ``runtraj status``, and reading results. Each result
is written straight to the store, while HYSPLIT itself still runs in a
workdir on local disk (``--compute-root`` or ``RUNTRAJ_COMPUTE_ROOT``).
Listing an object store takes longer than listing a disk, so
``runtraj status`` on a large project is slower.

To use another output directory than ``config.yaml`` names, without editing
it, give ``--output`` to ``runtraj run``, ``submit``, or ``status``, or open
the project with ``runtraj.Project(path, output="s3://my-bucket/slv-2021")``.

Reruns skip finished work
-------------------------

Before running, runtraj checks which simulations are finished and runs
only the rest. A simulation is complete when the result files its variant
makes exist in the output directory: the particle file, and the file of
each of its stages. If the particle file is missing, the transport model
runs again, and the stage files of every variant that shares those
particles are made again from the new ones.

So after an interruption, a failed Slurm task, or a new variant, run the
project again and only what is missing runs. ``runtraj status`` says what is
left (:doc:`checking`). To run everything again, pass
``skip_existing=False`` to ``project.run()``, or ``--no-skip`` to
``runtraj run``.

Changing a setting
------------------

Edit ``config.yaml`` and run again. Change a setting, including a default
the variant inherits or a setting of its met, and the variant's settings now
point at a folder that does not exist yet. The next run fills it, for every
release, and the old folder stays as it was.

``runtraj status`` lists every settings folder in the output directory, how
many result files it holds, which variants use it, and how a folder this
project does not use differs from the variant of its name. Here a project
with two variants, ``hrrr`` and ``hrrr-zi08: {ziscale: 0.8}``, ran, and then
``numpar`` went from 200 to 400 in its ``hysplit`` block, which both
variants inherit:

.. code-block:: text

   $ runtraj status ./my_project
   Project: /data/my_project  total=8  complete=0  failed=0  interrupted=0  pending=8
     hrrr: total=4  complete=0  failed=0  interrupted=0  pending=4
     hrrr-zi08: total=4  complete=0  failed=0  interrupted=0  pending=4
   Output: /data/my_project/output
     Used by this project: none yet
     Not used by this project (changed settings, a dropped variant, or another project sharing this output; runtraj never deletes one):
       particles  settings=hrrr-2e01ed       4 files  numpar: 200 (config: 400)
       particles  settings=hrrr-zi08-3ca0e9  4 files  numpar: 200 (config: 400)

Every simulation is pending again, and the two old folders are listed with
the setting that changed. The next run fills two new folders, which then
appear under "Used by this project". The counts at the top are simulations;
a folder's count is files, and one particles folder can serve several
variants. In Python the same table is
``project.folders()``.

To keep both results, give the new settings a new name instead: add
``hrrr-np400: {numpar: 400}`` and leave ``hrrr`` as it was. Only the new
variant runs, and you can select both when you read results
(:doc:`particles`).

Some settings do not change a result, so changing them makes no new folder:

- ``execution`` (including ``timeout`` and ``keep_workdir``) and ``output``
- HYSPLIT's ``directory``. What it holds is recorded instead: the checksum
  of ``hycs_std`` and of each data table, so another build or table is a
  new folder.
- where the met files are (``directory`` and ``subgrid_dir``)

Taking a variant out of ``config.yaml`` does not delete its results.
runtraj never deletes a folder, because another project may share the
output directory. When you are sure, delete a folder by hand.

Release ids and simulations
---------------------------

Each release has an id, which names its result files:

.. code-block:: text

   202101150800_-111.848_40.766_10            a point: time_lon_lat_altitude
   202101150800_-111.848_40.766_X0-3000msl    a column: X<bottom>-<top>
   202101150800_multi_7bb3ffdc53              a multipoint: a hash of its points

The time is UTC, to the minute. Heights above mean sea level add ``msl``;
heights above ground have no marker. A multipoint id does not change if you
reorder its points.

A simulation is a release id with a variant name, and a realization number
in an ensemble. ``project.simulation(release_id, "hrrr")`` looks one up.
In the output directory the release id is the file name, in a folder for
the day of the release time. Each particle file also stores its release, so
a scan of the whole ``particles/`` tree can tell the releases apart.

Opening a project again
-----------------------

After the project is made, the folder is all you need:

.. code-block:: python

   import runtraj

   project = runtraj.Project("./my_project")
   project.status()   # one row per simulation, with a "state" column

Opening a project only reads it. To change a setting, edit ``config.yaml``
and open the project again: a ``Project`` reads ``config.yaml`` once. To add
releases, add them and run:

.. code-block:: python

   project.add_releases(new_releases)     # returns the ids of the new ones
   project.run()

New releases are appended to ``releases.csv`` in the file's own columns.
Releases already in the file are left as they are (:doc:`releases`).

The same settings in Python
---------------------------

You can pass the blocks of ``config.yaml`` to
:meth:`Project.init <runtraj.Project.init>` as plain dictionaries:

.. code-block:: python

   import runtraj

   project = runtraj.Project.init(
       "./my_project",
       releases=releases,
       mets={"hrrr": "/data/arl/hrrr"},
       models={"hysplit": {"n_hours": -24, "numpar": 1000}},
       variants={"hrrr": {}, "hrrr-zi08": {"ziscale": 0.8}},
   )
   project.variants        # {"hrrr": Variant(...), "hrrr-zi08": Variant(...)}

``project.variants`` holds each variant with its settings in full: the
defaults merged with its overrides, checked by the transport model.

When a run fails or its particles look wrong, :doc:`checking` says where to
look.

Projects And Variants
=====================

A :term:`project` is a folder with your settings and your releases. Every
:term:`release` runs once under each :term:`variant`, a named set of
settings, and the results go to an :term:`output directory` the settings
name. We keep one project per study: a project reruns only what is missing
and never overwrites a result, so you can add releases and try other
settings as the study grows. Most projects need only the settings below,
and :doc:`../reference/configuration` lists every option.

The project folder
------------------

.. code-block:: text

   my_project/
     config.yaml        # your settings: meteorology, transport model, variants
     releases.csv       # your releases: where and when particles start
     tables/            # other inputs a stage reads (project.add_table)

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
       krand: 2
       seed: 42

   variants:
     hrrr: {}            # every release runs once, with these settings

The file is made of blocks, and no setting sits outside one:

``mets``
   Your meteorology, by name (:doc:`meteorology`). ``hrrr`` here is a name
   you choose for the folder of met files.
``models``
   The transport model and its settings (`Transport models`_). runtraj has
   no default model, so the block is required. HYSPLIT needs at least
   ``n_hours``; every other setting has a default.
``variants``
   What runs (`Variants`_).
``stages``
   What is made from each simulation's particles after the run, if
   anything (`Stages`_). Without it, a project makes particles only.
``execution``
   Where to run. Leave it out to run on your own computer (:doc:`running`).
``output``
   Where results go, ``output`` inside the project folder by default
   (:doc:`output`).

A setting written at the top of the file is refused with a message that
names the block it belongs in. A key runtraj does not know is an error that
names the nearest setting, so a typo such as ``numpr`` is caught when the
file loads, before anything runs.

Transport models
~~~~~~~~~~~~~~~~

You choose the transport model, a block under ``models`` named for it,
and install the model yourself. runtraj supports two:

.. list-table::
   :header-rows: 1
   :widths: 15 55 30

   * - Block
     - What it is
     - Settings
   * - ``hysplit``
     - `HYSPLIT <https://www.ready.noaa.gov/HYSPLIT.php>`_, NOAA's
       Lagrangian model, on ARL met files. Its ``mode`` also makes
       concentration grids or single trajectories.
     - :doc:`../reference/hysplit`
   * - ``flexpart``
     - `FLEXPART 11 <https://www.flexpart.eu>`_, a Lagrangian particle
       dispersion model you build yourself, on ERA5 GRIB files.
     - :doc:`../reference/flexpart`

A model from another package gets a block of any name with ``class:`` and
the import path of its class, such as ``class: mypkg.models.MyModel``
(:doc:`../reference/execution`, "Transport model"). A ``config.yaml`` without a
``models`` block is refused with a message that lists these.

The same project in Python
~~~~~~~~~~~~~~~~~~~~~~~~~~

:meth:`Project.init <runtraj.Project.init>` makes the folder, writes the
blocks you pass it to ``config.yaml`` and your releases to
``releases.csv``. The blocks are plain dictionaries:

.. code-block:: python

   import runtraj

   releases = [
       runtraj.PointRelease(
           time=f"2021-01-15 {hour:02d}:00",   # UTC
           longitude=-111.848,
           latitude=40.766,
           altitude=10,                       # meters above ground
       )
       for hour in (6, 7, 8, 9)
   ]
   project = runtraj.Project.init(
       "./my_project",
       releases=releases,
       mets={"hrrr": "/data/arl/hrrr"},
       models={"hysplit": {"n_hours": -24, "numpar": 1000, "krand": 2, "seed": 42}},
       variants={"hrrr": {}, "hrrr-zi08": {"ziscale": 0.8}},
   )

``Project.init`` runs once per project: it stops with a ``FileExistsError``
if the folder already has a ``config.yaml``. ``project.variants`` holds each
variant with its settings in full, the model block merged with the
variant's own, checked by the transport model.

The settings most people change
-------------------------------

.. list-table::
   :header-rows: 1
   :widths: 22 48 15 15

   * - Setting, under ``models: hysplit:``
     - What it does
     - Default
     - We use
   * - ``n_hours``
     - How many hours to follow the particles. Negative runs backward in
       time, from a measurement toward its sources. Positive runs forward,
       from a source toward where the air goes.
     - none
     - ``-24`` to ``-72``
   * - ``numpar``
     - Particles released per simulation. More particles sample the
       turbulence better and take longer.
     - ``2500``
     - ``1000``
   * - ``krand`` and ``seed``
     - How HYSPLIT draws its random numbers. ``krand: 2`` with a ``seed``
       makes the particles the same on every run.
     - chosen by HYSPLIT
     - ``2`` and ``42``

:doc:`choosing_settings` says how we choose each for a study, and what
HYSPLIT chooses for you when you leave the others out.

Variants
--------

Every release runs once per variant. A variant has a name, a met, and any
settings that differ from the model's block, which are the variant's
:term:`defaults`. Only the variants listed run, and an entry with no
overrides (``hrrr: {}``) runs the model's block unchanged. Add entries to
run the same releases under other settings:

.. code-block:: yaml

   variants:
     hrrr: {}                     # the model block, with the hrrr met
     hrrr-zi08: {ziscale: 0.8}    # mixed-layer height times 0.8
     hrrr-np3k: {numpar: 3000}    # three times the particles

One release under one variant is a :term:`simulation`, so 100 releases and
three variants make 300 simulations. A variant may set:

``met``
   Which met to use. A variant named after a met, as ``hrrr`` is here,
   uses that met; any other name needs ``met: hrrr`` when ``mets`` has more
   than one entry. The names in these examples start with the met's only
   by habit.

``realizations``
   Run the variant ``N`` times, as realizations ``0`` to ``N - 1`` of one
   ensemble, each with its own random numbers. With ``krand: 2`` and a
   ``seed``, realization ``k`` runs with ``seed + k``, so the ensemble can
   be made again. Raising ``N`` later only adds realizations.

``model``
   Which block under ``models`` it runs. You need it only when ``models``
   has more than one entry.

Here one project runs every release twice, under HYSPLIT on HRRR and under
FLEXPART on ERA5, each variant naming its model and its met:

.. code-block:: yaml

   mets:
     hrrr: /data/arl/hrrr     # ARL files, for HYSPLIT
     era5: /data/era5         # GRIB files from flex_extract, for FLEXPART

   models:
     hysplit:
       directory: /path/to/hysplit
       n_hours: -24
       numpar: 1000
     flexpart:
       directory: /path/to/flexpart
       eta: true
       n_hours: -24
       numpar: 1000

   variants:
     hrrr: {model: hysplit}     # each variant is named after its met
     era5: {model: flexpart}

Each block names its model's folder. HYSPLIT can do without it when its
``hycs_std`` is on ``PATH`` (:doc:`../getting_started/installation`);
FLEXPART needs it.

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

Most projects have no stage: they make particles, which you then read
(:doc:`particles`). A :term:`stage` makes one more file per simulation from
its particles, after the transport model runs. runtraj has none built in: a
stage comes from another package, and ``config.yaml`` names its class by
import path, in a block named for the stage. PYSTILT's footprint is an
example:

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

Edit ``config.yaml`` and run again. Each result is kept in a
:term:`settings folder` named for the settings it was made with, so a
changed setting, including one the variant inherits from the model block or
a setting of its met, points the variant at a folder that does not exist
yet. The next run fills it, for every release, and the old folder stays as
it was.

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
variants. In Python the same table is ``project.folders()``.

To keep both results, give the new settings a new name instead: add
``hrrr-np400: {numpar: 400}`` and leave ``hrrr`` as it was. Only the new
variant runs, and you can read both side by side (:doc:`particles`).

Some settings do not change a result, so changing them makes no new folder:

- ``execution`` (including ``timeout`` and ``keep_workdir``) and ``output``
- HYSPLIT's ``directory``. What it holds is recorded instead: the checksum
  of ``hycs_std`` and of each data table, so another build or table is a
  new folder.
- where the met files are (``directory`` and ``subgrid_dir``)

Taking a variant out of ``config.yaml`` does not delete its results.
runtraj never deletes a folder, because another project may share the
output directory. When you are sure, delete a folder by hand.

Opening a project again
-----------------------

After the project is made, the folder is all you need:

.. code-block:: python

   import runtraj

   project = runtraj.Project("./my_project")
   project.status()   # one row per simulation, with a "state" column

Opening a project only reads it. A ``Project`` reads ``config.yaml`` once,
so after editing it, open the project again. To add releases, add them and
run:

.. code-block:: python

   project.add_releases(new_releases)     # returns the ids of the new ones
   project.run()

New releases are appended to ``releases.csv`` in the file's own columns.
Releases already in the file are left as they are (:doc:`releases`).

When a run fails or its particles look wrong, :doc:`checking` says where to
look.

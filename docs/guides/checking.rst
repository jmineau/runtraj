Checking A Run
==============

A batch of thousands of runs is quick to start and slow to find wrong. We
run ``runtraj check`` before every batch, because it finds the common
mistakes in the inputs in seconds, and ``runtraj status`` after, because it
says which simulations need another look and why. When a run finishes but
its particles look wrong, the last section lists what to check, in order.

The examples on this page that run use a small sample project: four
releases an hour apart on the morning of 15 January 2021 at one site in Salt
Lake City, 24-hour backward runs with 100 particles, and one variant,
``hrrr``. It was made as in :doc:`projects`, "The same project in Python".

.. This page runs when the docs are built, on the sample project of
   docs/_ext/docs_examples.py (four releases, all complete).

.. ipython:: python
   :suppress:

   import os

   import docs_examples

   docs_cwd = docs_examples.temp_workdir(sample=True)

Before a run
------------

Most failures of a new project come from its inputs: a met folder with a
typo, met files that miss some release's hours, a release outside the met's
domain. ``runtraj check`` reads the project's files and the headers of the
met files, and runs and downloads nothing:

.. code-block:: text

   $ runtraj check ./my_project
   Project: /data/my_project  releases=6
   Variants: hrrr (met hrrr, particles only), hrrr-zi08 (met hrrr, particles only)

   check       variant          release                          message
   model       hrrr, hrrr-zi08                                   No hycs_std on PATH. runtraj does not include HYSPLIT: get it (the installation guide says where), then set directory (under models: hysplit: in config.yaml) to the HYSPLIT folder, or put the folder that holds an executable hycs_std on PATH.
   met_files   hrrr, hrrr-zi08  202101140200_-111.848_40.766_10  no met file for 2021-01-13 02:00 to 2021-01-14 02:00
   met_domain  hrrr, hrrr-zi08  202101150800_-115.5_40.766_10    longitude -115.5 and latitude 40.766 are outside the met's domain, the grid of /data/arl/hrrr/20210114_06-11_hrrr.crop_-114.00_39.00_-110.00_42.00: HYSPLIT cannot start particles there.

   Found 3 problems: model 1, met_files 1, met_domain 1

Here ``hycs_std`` is not on ``PATH``, one release needs met files from
hours the met folder does not have, and another lies west of the met's
domain. Variants that share what a check
looks at share its rows. The command exits with 0 when it finds nothing and
1 when it lists anything, so a batch script can stop on it.

.. list-table::
   :header-rows: 1
   :widths: 18 42 40

   * - Check
     - What it found
     - What to do
   * - ``releases``
     - The project has no releases: ``releases.csv`` is missing or holds
       only its header.
     - Add releases (:doc:`releases`).
   * - ``model``
     - No ``exec/hycs_std`` in ``directory``, or one that is not
       executable; without ``directory``, no ``hycs_std`` on ``PATH``.
     - Get HYSPLIT and set ``directory`` or ``PATH``
       (:doc:`../getting_started/installation`).
   * - ``release_refused``
     - The transport model cannot run a release, and such a run fails as
       ``RELEASE_REFUSED``. HYSPLIT's refusals are listed in its failure
       table (:doc:`../reference/hysplit`); the usual one is a multipoint
       release whose points share a horizontal location.
     - For HYSPLIT, use a ``ColumnRelease`` for a vertical column, or one
       ``PointRelease`` per height (:doc:`releases`).
   * - ``setting_refused``
     - A setting the HYSPLIT build here would ignore or change without a
       word, which refuses every release (``release`` is empty): ``krand``
       10 to 13 before HYSPLIT 5.1.0, and the wind error settings in the
       concentration mode from 5.3.0. A build that prints no version is
       taken to be the oldest. Such a run fails as ``RELEASE_REFUSED``.
     - Change the setting, or use another HYSPLIT build
       (:ref:`hysplit-versions`).
   * - ``met_directory``
     - A met's ``directory`` does not exist or holds no met file.
     - Correct ``directory`` under ``mets:``. A met with ``download`` makes
       its folder when a run needs it, so it is not checked.
   * - ``met_files``
     - A release's run needs a met file that is not there, or files of two
       products hold one of its times. The message names the hours or the
       two files.
     - Add the files, or keep each product in its own folder
       (:doc:`meteorology`).
   * - ``met_domain``
     - A release lies outside the grid of the met files its run starts in,
       where HYSPLIT cannot start particles. The grid is read from a file's
       header.
     - Use met files whose domain holds the release, or check the sign of
       its longitude.
   * - ``met_crop``
     - With ``subgrid_enable`` on, a release lies outside
       ``subgrid_bounds``.
     - Widen ``subgrid_bounds`` to hold the whole run, not only the
       release.
   * - ``met_count``
     - A release's run needs more than 128 met files, more than HYSPLIT
       reads in time.
     - Use met files that each hold more hours, such as daily ones, or
       shorter runs.
   * - ``met_periods``
     - A file the releases need holds one time period, which HYSPLIT
       cannot read.
     - Use files that each hold at least two time periods.
   * - ``met_cut``
     - A file the releases need ends partway through a time step, so it is
       damaged.
     - Download or crop the file again.
   * - ``met_label``
     - Two mets of one transport model record the same settings, such as
       two folders of WRF files with one source id, so variants that differ
       only in their met would share one run. A run refuses them too.
     - Give each met a ``label`` of its own under ``mets:``
       (:doc:`meteorology`).
   * - ``zsfc``
     - ``varsiwant`` lacks ``zsfc``, the terrain height, and a release
       given above sea level needs it. The message says why.
     - Add ``zsfc`` to ``varsiwant`` before the runs. It is a run setting,
       so adding it later makes the particles again in a new folder.

A stage adds checks of its own inputs, under names it chooses: a stage that
sums particles onto a grid may list a release outside its grid. ``runtraj
check`` reads the headers of the met files, not every record, so a file
damaged in the middle passes the check and fails the run that opens it.

In Python, ``project.check()`` returns the same table, one row for each
thing found. It is empty for the sample project, which has everything its
runs need:

.. ipython:: python

   import runtraj

   project = runtraj.Project("./my_project")
   problems = project.check()
   problems

The table is a pandas DataFrame, so ``problems[problems.check ==
"met_files"]`` selects the releases that lack met files.

Where each simulation stands
----------------------------

``runtraj status`` counts the simulations of a project by state and the
failed ones by reason, then lists the settings folders in the output
directory (:doc:`projects`, "Changing a setting"). With failures, its first
lines look like this:

.. code-block:: text

   $ runtraj status ./my_project
   Project: /data/my_project  total=1200  complete=1150  failed=42  interrupted=0  pending=8
   failed: MET_COVERAGE 34, TIMEOUT 8  (why: each one's .failure.yaml, under /data/my_project/output/logs)

In Python, ``project.status()`` has one row per simulation, so you can
select the failures and read why:

.. ipython:: python

   status = project.status()
   status.state.value_counts()
   status[status.state == "failed"][["release", "variant", "reason", "message"]]

Every simulation of the sample project is complete, so there are no
failures to list. The state comes from the files in the output directory,
so it is the same whoever asks:

.. list-table::
   :header-rows: 1
   :widths: 15 45 40

   * - State
     - What it means
     - What to do
   * - ``complete``
     - Its result files exist: the particles, and the file of each stage
       its variant has.
     - Nothing. A rerun skips it.
   * - ``failed``
     - The last run failed, and a failure record says why (below).
     - Fix the cause and run again. Only the unfinished simulations run.
   * - ``interrupted``
     - The run started and was stopped before it finished: a time limit,
       preemption, Ctrl-C, or a lost connection.
     - Run again. A Slurm task that gets its two-minute warning is
       requeued by its job script.
   * - ``pending``
     - It has not run yet, or its particles exist and a stage file does
       not.
     - Run the project.

``runtraj run`` exits with 0 when all its simulations are complete, 1 when
some failed and 3 when some were interrupted, so a script or a scheduler can
rerun on 3 and stop to look on 1 (:ref:`exit-codes` lists every code).

Why a simulation failed
-----------------------

A failed simulation does not stop the others. The worker writes a failure
record beside the run's log and goes on, and ``sim.failure`` reads it:

.. code-block:: pycon

   >>> failed = status[status.state == "failed"]
   >>> sim = project.simulation(failed.release.iloc[0], "hrrr")
   >>> sim.failure
   {'stage': 'particles', 'reason': 'MET_COVERAGE',
    'message': 'HYSPLIT: start point not within (x,y,t) any data file',
    'time': '2026-10-03T21:14:05+00:00'}

``stage`` says whether the transport model failed (``particles``, which
fails every variant that shares those particles) or only a stage, by its
name. ``message`` is the line of the log that says what went wrong. Three
reasons cover most failures:

``MET_COVERAGE``
   The meteorology does not cover the run: a file is missing or damaged, or
   the met ran out partway. The message names the file or the hours.
   Download or crop the file again, or add the missing hours. A release
   outside the met's grid fails this way too.

``EXIT_STATUS``
   HYSPLIT was killed or exited with an error. A ``SIGKILL`` that runtraj
   did not send is often the out-of-memory killer, so ask for more memory
   per task or run fewer releases at once.

``TIMEOUT``
   The run took longer than ``timeout`` under ``execution:``. Raise it, or
   look for a slow node or a met file read over a slow filesystem.

The other reasons are rarer. Every reason, with what to do, is in :doc:`../reference/hysplit` ("Failure
reasons"). An error that is not a known reason is recorded by its class,
such as ``ValueError``, with its traceback. That is usually a bug, and worth
reporting.

``sim.log`` is the transport model's log. A failed run's :term:`workdir`,
the folder HYSPLIT ran in, is kept under ``scratch/`` in the output
directory, and ``sim.kept_workdir`` says where. For HYSPLIT it holds
``CONTROL``, ``SETUP.CFG`` and ``MESSAGE``, enough to rerun ``hycs_std`` by
hand. The particle file is left out, since it can take hundreds of
megabytes. Set ``keep_workdir: true`` under ``execution:`` to keep every
run's workdir whole, particle file included.

My particles look wrong
-----------------------

Start with a map, because it shows where the air went:

.. ipython:: python

   sim = project.simulation("202101150800_-111.848_40.766_10", "hrrr")
   particles = sim.particles

   @savefig checking_particles.png width=6in
   particles.runtraj.plot.map(release=sim.release);

.. ipython:: python
   :suppress:

   os.chdir(docs_cwd)

The particles of this run went north-northwest up the valley until they
reached the northern edge of the cropped met files, where they stopped. Then
check, in order:

The time
   Release times are UTC. A time in local time shifts the release, and
   with it every trajectory.

The release height
   Altitudes are above ground unless the release says
   ``altitude_ref="msl"``. A tower inlet given above sea level as if above
   ground starts the particles high above the mixed layer, far from the
   surface.

The length of the run
   ``n_hours`` sets how far the particles go. Particles also stop early
   when they leave the met's domain or its crop, and the run is still
   complete. ``particles["age"].abs().max() / 3600`` is how many hours they
   went. If that is short, widen the met's domain or ``subgrid_bounds``
   (:doc:`choosing_settings`).

The number of particles
   Few particles sample the turbulence poorly, so a map or a sum over them
   is patchy. Raise ``numpar`` (:doc:`choosing_settings`).

The direction
   A negative ``n_hours`` runs backward, from a measurement toward its
   sources; a positive one runs forward from a source. A sign the wrong
   way round sends the particles downwind of where you meant.

HYSPLIT Integration
===================

A variant whose model block is ``hysplit`` runs `HYSPLIT
<https://www.ready.noaa.gov/HYSPLIT.php>`_ (FLEXPART is built in too,
:doc:`flexpart`). The worker gets it as
:class:`runtraj.models.hysplit.HysplitModel` (see :doc:`execution`), which
picks the met files, writes HYSPLIT's input files, runs ``hycs_std``, and
reads the particles it writes, or, with ``mode: concentration``, its
concentration grids (:ref:`hysplit-concentration`). With ``mode:
trajectory`` it runs ``hyts_std`` instead (:ref:`hysplit-trajectory`).

Config
------

:class:`runtraj.models.hysplit.HysplitConfig` holds every setting that
shapes the particles HYSPLIT makes. In ``config.yaml`` they are the keys
of the ``hysplit`` block under ``models:``.

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.models.hysplit.HysplitConfig

The HYSPLIT build
-----------------

runtraj includes no HYSPLIT. :func:`~runtraj.models.hysplit.find_hycs_std`
returns the ``hycs_std`` a run uses: ``exec/hycs_std`` under ``directory``, or else the
first on ``PATH`` (:doc:`../getting_started/installation`).
:meth:`HysplitModel.sha256 <runtraj.models.hysplit.HysplitModel.sha256>`
is its checksum, part of each run's settings, and
:meth:`HysplitModel.version <runtraj.models.hysplit.HysplitModel.version>`
the version it prints when it starts, recorded beside them.
:func:`~runtraj.models.hysplit.data_tables` gives HYSPLIT's data tables a
run uses, from the ``bdyfiles/`` folder of ``directory`` or beside the
``hycs_std`` on ``PATH``.

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.models.hysplit.find_hycs_std
   runtraj.models.hysplit.find_program
   runtraj.models.hysplit.data_tables

Running HYSPLIT
---------------

:func:`~runtraj.models.hysplit.write_inputs` writes everything
``hycs_std`` reads into a folder: ``CONTROL``, ``SETUP.CFG``, and the error
and mixed-layer files when the settings call for them. It takes what
``CONTROL`` starts from: a release time and release locations (longitude,
latitude and height, like ``release.coords()``; the ``CONTROL`` file lists
each latitude first, and the writer swaps them), with ``kmsl`` 0 for heights
above ground and 1 above sea level, as ``SETUP.CFG``'s ``KMSL``.
:class:`~runtraj.models.hysplit.HysplitModel` passes a release's time,
release points (``release.coords()``) and ``altitude_ref``.
:func:`~runtraj.models.hysplit.run_hycs_std` runs ``hycs_std`` in the
folder, and :func:`~runtraj.models.hysplit.read_particle_dat` reads its
particle file as a particle table. Use them to look at or change
the input files of a run. A project run then adds the release heights
(:doc:`particles`); by hand:

.. code-block:: python

   from pathlib import Path

   from runtraj.particles import add_release_heights
   from runtraj.models.hysplit import (
       HysplitConfig,
       HysplitModel,
       read_particle_dat,
       run_hycs_std,
       write_inputs,
   )
   from runtraj.models.hysplit.driver import PARTICLE_FILE

   config = HysplitConfig(n_hours=-24, numpar=200)
   write_inputs(
       "run",
       release_time=release.time,
       locations=release.coords(),
       config=config,
       met_files=met_files,
       kmsl=1 if release.altitude_ref == "msl" else 0,
   )
   run_hycs_std(Path("run"))
   particles = read_particle_dat(Path("run") / PARTICLE_FILE, config.varsiwant)
   particles = add_release_heights(particles, release, HysplitModel().release_rows)

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.models.hysplit.write_inputs
   runtraj.models.hysplit.run_hycs_std
   runtraj.models.hysplit.read_particle_dat

.. _hysplit-concentration:

The concentration mode
----------------------

With ``mode: concentration`` runtraj writes HYSPLIT's own concentration
output, ``ICHEM`` from ``ichem`` (0 unless set), with HYSPLIT's dispersion
scheme (``IDSP`` unset) in place of ``ICHEM=8`` and ``IDSP=2``. The pollutants and grids are
written to ``CONTROL``:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - In ``CONTROL``
     - In runtraj
   * - Lines 10 to 14: the pollutants, their emission rate, hours and start
     - ``pollutants``: ``name``, ``rate``, ``hours``; the start is the
       release time
   * - Lines 15 to 25: the grids, their center, spacing, span, file,
       levels, sampling start, stop and interval
     - ``grids``: ``center_lon`` and ``center_lat`` (unset, ``0.0 0.0``,
       the release), ``spacing_*``, ``span_*``, ``levels``, ``sampling``
       and ``sampling_interval`` (under an hour, any number of minutes;
       from an hour up, whole hours, which is how HYSPLIT reads it); the
       file is ``cdump_<name>`` in the workdir, and the samples cover the
       whole run
   * - Lines 26 to 31: deposition, wet removal, decay and resuspension of
       each pollutant
     - The other fields of each pollutant, in HYSPLIT's units

:func:`~runtraj.models.hysplit.read_cdump` reads one grid's file, packed
(``cpack`` 1, HYSPLIT's default, or 3) or not (``cpack: 0``), as a
Dataset of :doc:`concentration`. A file of one point per grid (``cpack:
2``) is refused. A run fails as ``NO_CONCENTRATION_DATA`` when a grid has
no file, or a file with no sample, as when ``sampling_interval`` is longer
than the run, or fewer samples than the run holds: its length divided by
the interval, in whole intervals. HYSPLIT keeps writing samples,
of zeros, after its particles are gone, so a run whose met ran out after
it started is not found this way, and runtraj does not check a
concentration run for it, as it does a particle run (its files are checked
before the run starts).

HYSPLIT stops releasing when it holds ``MAXPAR`` particles. A particle run
releases in one time step, and a concentration run over each pollutant's
``hours``, again every ``qcycle`` hours; HYSPLIT rounds the particles of
each hour and each time step up, for each pollutant at each location, so
it asks for more than ``numpar`` (2010 for 2000 from one location over an
hour, 2070 from three). With ``maxpar`` unset a concentration run gets
``numpar`` and that rounding for every cycle
(:meth:`~runtraj.models.hysplit.HysplitConfig.maxpar_for`), so the grids
hold all that was released. Set ``maxpar`` only above that.

``ichem`` is recorded in a concentration run's settings, 0 when unset. No
value changes the form of the file, only what a cell or a pollutant of it
means, which the file does not say:
:func:`~runtraj.models.hysplit.read_cdump` is told the ``ichem`` and
``cmass`` the run had. What each value does and needs, as seen in runs of
HYSPLIT 5.4.2:

.. list-table::
   :header-rows: 1
   :widths: 8 92

   * - ``ichem``
     - What HYSPLIT does, and what runtraj refuses with it
   * - 1
     - Names a pollutant for each ``CONTROL`` location, ``0001``,
       ``0002``, ... in hexadecimal, read as the dimension ``source``. It
       drops every pollutant after the first, so more than one is
       refused. A column release, two locations that are one line source,
       left the first empty and filled the second: refused before the run
       (``RELEASE_REFUSED``). ``nbptyp`` and ``maxdim`` must be 1.
   * - 2
     - Reads ``CHEMRATE.TXT``, a line per conversion (the two pollutants'
       numbers, the fraction per hour, the weight ratio), which runtraj
       writes from ``conversions`` on every run: without the file HYSPLIT
       converts pollutant 1 to 2 at 0.10 per hour, so an empty
       ``conversions`` is refused. With ``maxdim: 1`` HYSPLIT prints an
       error, exits with status 0 and leaves a file with no sample:
       ``maxdim`` must be the number of pollutants.
   * - 5
     - Deposits whole particles with a probability. HYSPLIT stops on
       ``maxdim`` above 1, and changes ``initd`` to 0 itself: both are
       refused.
   * - 6
     - Divides each particle's mass by the air density at its level
       before it is counted. The ``units`` are ``mass kg-1``, or ``mass m3
       kg-1`` with ``cmass: 1``; deposition at ``z = 0`` is not divided.
   * - 9
     - Reads the first of a grid's levels as a percentage of the mixed
       layer depth and counts from the ground to that share of it, at
       each particle (10 m at least). ``z`` keeps the percentage and says
       so in its ``long_name``. A level of 0 first became a 10 m level
       and no deposition was counted, so the first level must be 1 to
       100.
   * - 10
     - Names a pollutant for each emission cycle by the tenths of an hour
       from the start of the run, read as the dimension
       ``release_time``. It makes ``n_hours // qcycle`` cycles and
       releases nothing after the last; with half-hour cycles two of
       eight were empty; in a backward run only the first was released.
       So ``qcycle`` is whole hours that divide ``n_hours``, the run is
       forward, and the pollutant's ``hours`` are ``qcycle`` at most.
       HYSPLIT stops on ``qcycle: 0`` and on an ``efile``.
   * - 3, 4, 7, 11
     - Refused: dust emission needs an emission and an area on each
       location; 4 counts on the met model's grid for HYSPLIT's chemistry
       builds; 7 needs momentum fluxes in the met files; 11 needs a
       ``DAUGHTER.TXT`` from HYSPLIT's preprocessor.
   * - 8
     - Refused here: it is ``mode: particles``, which takes ``ichem: 8``
       or none.
   * - others
     - Refused: HYSPLIT ran ``ICHEM=12`` as 0 without a message.

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.models.hysplit.Pollutant
   runtraj.models.hysplit.ConcentrationGrid
   runtraj.models.hysplit.Conversion
   runtraj.models.hysplit.read_cdump

.. _hysplit-trajectory:

The trajectory mode
-------------------

With ``mode: trajectory`` runtraj runs HYSPLIT's trajectory program,
``hyts_std``, from ``exec/`` of the same HYSPLIT folder (or ``PATH``), and
its SHA-256 is the one in the run's settings. ``CONTROL`` has the lines
both programs read (the release time and locations, ``n_hours``,
``w_option``, ``z_top``, the met files), then ``./`` and ``tdump``, where
the trajectories are written.

``SETUP.CFG`` is one namelist for both programs, and ``hyts_std`` reads
the dispersion model's entries too: ``ICHEM`` and ``IDSP`` change its
trajectories. So a trajectory run's ``SETUP.CFG`` holds only:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - In ``SETUP.CFG``
     - In runtraj
   * - ``KAGL``
     - Fixed at 1: heights above ground, ``zagl``. A config that sets
       ``kagl`` to another value is refused.
   * - ``KMSL``
     - The release's ``altitude_ref``
   * - ``TOUT``
     - ``tout``, 60 unless set. Under an hour it must divide 60;
       ``hyts_std`` lowers any other value to one that does, so the
       config refuses it.
   * - ``TM_TPOT``, ``TM_TAMB``, ``TM_RAIN``, ``TM_MIXD``, ``TM_RELH``,
       ``TM_SPHU``, ``TM_MIXR``, ``TM_TERR``, ``TM_DSWF``
     - The ``tm_`` settings, each a column of the table
       (:doc:`trajectories`). ``hyts_std`` always writes the pressure.
   * - ``DELT``, ``TRATIO``, ``MGMIN``, ``KHMAX``, ``K10M``, ``KMIXD``,
       ``KMIX0``, ``WBBH``, ``WBWF``, ``WBWR``
     - The settings of those names. ``kmix0`` is 0 unless set, the
       default of ``hyts_std``.

Every other setting is the dispersion model's and is left out of a
trajectory run, neither written nor recorded
(:data:`~runtraj.models.hysplit.config.TRAJECTORY_SETTINGS`). ``nstr`` and
``nver`` (restarts and splits) are refused: a release starts its own
trajectories, and runtraj does not read a split run's file.

``hyts_std`` starts a trajectory at every location in ``CONTROL``, so a
multipoint release may hold two points at one place, which ``hycs_std``
would join into a line source. A column release is refused
(``RELEASE_REFUSED``), since there is no line source to make of it.

:func:`~runtraj.models.hysplit.read_tdump` reads the file by the widths of
its fields. A run fails as ``NO_TRAJECTORIES_DATA`` when there is no file
or no row in it, and as ``MET_COVERAGE`` when it holds fewer trajectories
than the release has locations: ``hyts_std`` leaves out a location outside
the met's domain and numbers the rest from 1. A trajectory that leaves the
domain later ends there, and its run is complete. Met that runs out is
found as in any run, from the files before it starts and from HYSPLIT's
warning after.

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.models.hysplit.read_tdump

Reading the input files
-----------------------

:class:`~runtraj.models.hysplit.ControlFile` holds what a ``CONTROL`` file
says, and :meth:`~runtraj.models.hysplit.ControlFile.read` reads one.
:func:`~runtraj.models.hysplit.read_namelist` reads a ``SETUP.CFG`` into a
dict by lowercase name. Use them to look at the input files of a run that
was made without runtraj.

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.models.hysplit.ControlFile
   runtraj.models.hysplit.read_namelist

Releases
--------

HYSPLIT joins release points at one latitude and longitude into a vertical
line source, so it refuses a multipoint release whose points share a
horizontal location
(:func:`~runtraj.models.hysplit.releases.check_releases`): ``runtraj check``
lists it, and its run fails as ``RELEASE_REFUSED`` before ``hycs_std``
starts. HYSPLIT 5.1.0
writes no particle row at ``age = 0``, so
:func:`~runtraj.models.hysplit.releases.release_rows` says where each
particle of a column or multipoint release started: particle ``k`` of a
column in slab ``k``, and a multipoint release's particles matched to a
point from their first row (:doc:`particles`).

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.models.hysplit.releases.check_releases
   runtraj.models.hysplit.releases.release_rows

Failure reasons
---------------

When a run ends, runtraj reads HYSPLIT's log for the messages it knows.
When the particles stopped before ``n_hours``, it also checks where: a
particle more than 5 grid cells inside the met's domain means the met ran
out (``MET_COVERAGE``), since particles that leave the domain end at its
edge. A known failure, a timeout, or a run
without particle output raises a
:class:`~runtraj.exceptions.SimulationError` whose ``reason`` is a
:class:`runtraj.models.hysplit.FailureReason`. The worker records it, and
:attr:`runtraj.Simulation.failure` and ``runtraj status`` report it.

Each reason, what happened, and what to do:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Reason
     - What happened, and what to do
   * - ``MET_COVERAGE``
     - The meteorology does not cover the run. The message says which:

       - ``No met file ... for <hours>``: a file the run needs is missing.
         Check the met's ``directory`` against the files on disk. A backward run needs files from ``n_hours`` before
         the release time to just after it, a forward run from the
         release time to just after ``n_hours`` later (:doc:`/guides/meteorology`).
         ``runtraj check`` finds these before a run.
       - ``The met file ... is damaged`` or ``The met files hold no time
         step between ...``: a file is there but cut short, written
         partway, or has records lost to null bytes, so it lacks hours the
         run needs. Download or crop it again. runtraj checks each file
         before HYSPLIT runs (:meth:`arlmet.File.check`).
       - ``The met file ... holds one time period``: HYSPLIT cannot read a
         file with one time period. Use files that each hold at least two
         (``runtraj check`` finds this before a run).
       - ``no more meteorology``: the particles reached a time that no met
         file holds. HYSPLIT says so in the ``WARNING`` file of the kept
         workdir.
       - ``The particles stopped ... h into the ... h run, up to ... grid
         cells inside the met's domain``: the particles stopped before
         ``n_hours`` and at least one was more than 5 grid cells from
         every side of the met grid.
         Particles that leave the domain end within 2 cells of its edge,
         so the met ran out, though HYSPLIT may have written no warning
         and ``Complete Hysplit``. Look for a met file with fewer hours
         than its name says, and download or crop it again. runtraj
         checks this only when ``outdt`` is 0, the default: with fewer
         rows, a particle's last row can be far from where it left.
       - A HYSPLIT message, such as ``start point not within (x,y,t) any
         data file``: the release is outside the files, in time or in
         space. ``source point off grid`` says the release is outside the
         met's grid (check the sign of its longitude; ``runtraj check``
         lists such a release as ``met_domain``), and ``Start height
         above mdl domain`` that it is above the met's top; the line gives the
         height and the top.
   * - ``VARYING_MET_INTERVAL``
     - The met files do not all have the same time step, such as hourly
       and 3-hourly files mixed in one directory. Keep one product per
       met.
   * - ``NO_PARTICLE_DATA``
     - The transport model ran but wrote no particles. Read the log
       (``sim.log``): it usually says why.
   * - ``NO_CONCENTRATION_DATA``
     - A concentration run wrote no file for a grid, one with no sample,
       or one with fewer samples than the run holds. A
       ``sampling_interval`` longer than the run gives no sample; for
       anything else, read the log.
   * - ``NO_TRAJECTORIES_DATA``
     - A trajectory run wrote no ``tdump`` file, or one with no row. Read
       the log.
   * - ``FORTRAN_RUNTIME_ERROR``
     - HYSPLIT stopped with a Fortran error, which the message names. Read
       the log and the kept workdir's ``MESSAGE`` file.
   * - ``PARTICLE_LIMIT``
     - HYSPLIT reached ``maxpar`` before every location of a multipoint
       release got its particles, so the last ones would have fewer or
       none. Leave ``maxpar`` unset, or raise it (:doc:`/guides/releases`).
   * - ``EXIT_STATUS``
     - ``hycs_std`` was killed by a signal, or exited with a non-zero
       status, and its log holds no other known message. The message names
       the signal or the status, and the log ends with the last lines of
       HYSPLIT's ``MESSAGE`` file. A ``SIGKILL`` that nothing in runtraj
       sent is often the kernel's out-of-memory killer: ask for more
       memory per task, or run fewer releases at once. The particles
       HYSPLIT had written are not kept, and running again runs the
       simulation again.
   * - ``RELEASE_REFUSED``
     - A multipoint release whose points share a horizontal location,
       which HYSPLIT would join into a line source. Use a
       :class:`~runtraj.ColumnRelease` for a column, or one
       :class:`~runtraj.PointRelease` per height (:doc:`/guides/releases`).
   * - ``TIMEOUT``
     - The run took longer than ``timeout`` under ``execution:``. Raise it,
       or look for a slow node or a met file read over a slow filesystem.
   * - an error's name
     - Any other error is recorded by its class, such as ``ValueError``,
       with its traceback. That is usually a bug or a setup problem worth
       reporting.


.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.models.hysplit.FailureReason

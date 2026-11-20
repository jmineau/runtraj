The Particle Table
==================

.. currentmodule:: runtraj.particles

A transport model run gives one table: a row per particle per output step.
Everything after the run reads it: the stages, the plots, and the particle
files in the output directory. ``sim.particles`` and
:func:`runtraj.run_trajectories` return it as a pandas DataFrame.
:doc:`layout` says how a particle file stores it.

Every table has five columns, :data:`PARTICLE_SCHEMA`:

.. list-table::
   :header-rows: 1
   :widths: 14 22 64

   * - Column
     - Units
     - Meaning
   * - ``particle``
     - none
     - Particle number, from 1 to ``numpar``. A particle keeps its number
       for the whole run. HYSPLIT calls it ``indx``. Stored as int32 and
       read back as float64.
   * - ``age``
     - minutes
     - Time since release. Negative for a backward run, positive for a
       forward one. HYSPLIT calls it ``time``. Stored as whole minutes,
       int32, and read back as float64.
   * - ``lon``
     - degrees east
     - Longitude of the particle (HYSPLIT's ``long``).
   * - ``lat``
     - degrees north
     - Latitude of the particle (HYSPLIT's ``lati``).
   * - ``zagl``
     - m
     - Height of the particle above ground.

The release row
---------------

A model should write each particle's release as a row at ``age = 0``. That
row is where the particle started, so it says which release point of a
multipoint release the particle left from, and which slab of a column
release (:func:`add_release_heights`). A model that writes no such row
says where each particle started through its ``release_rows``
(:class:`~runtraj.models.TransportModel`); without either, a column or
multipoint release fails with the reason ``NO_RELEASE_ROWS``.

HYSPLIT 5.1.0 writes no release row: its first row is one time step after
release. Its ``release_rows`` matches each particle to a release point from
its first position, and takes a column's particles to be released bottom to
top in ``particle`` order. A HYSPLIT build that writes the release rows
(``directory``) makes the matching exact.
FLEXPART writes no release row either: its first row is one output step
after release (:doc:`flexpart`).

Columns the model adds
----------------------

HYSPLIT writes the variables in ``varsiwant`` under HYSPLIT's lowercase
four-letter names, except ``indx``, ``time``, ``long``, and ``lati``, which
become the columns above. The allowed names are listed under ``varsiwant`` in
:doc:`configuration`; the config refuses any other name when it is read.
Some of them:

.. list-table::
   :header-rows: 1
   :widths: 14 22 64

   * - Column
     - Units
     - Meaning
   * - ``foot``
     - ppm per (µmol m⁻² s⁻¹)
     - The particle's sensitivity to surface fluxes accumulated over the
       output step, which HYSPLIT computes with ``ICHEM=8`` and its
       ``veght`` (Lin et al., 2003).
   * - ``mlht``
     - m
     - Mixed-layer height above ground.
   * - ``dens``
     - kg m⁻³
     - Air density.
   * - ``samt``
     - minutes
     - Time the particle spent in the surface layer during the step.
   * - ``sigw``
     - m s⁻¹
     - Standard deviation of the vertical wind.
   * - ``tlgr``
     - s
     - Lagrangian time scale.
   * - ``pres``
     - hPa
     - Pressure.
   * - ``zsfc``
     - m
     - Terrain height above sea level.
   * - ``zcfx``
     - m
     - Vertical displacement by convective flux along the trajectory,
       summed over the steps (the Grell scheme, ``capemin: -2``). HYSPLIT
       reads this name only: ``zfx1`` stops it, and the config refuses it.

FLEXPART writes ``mlht``, ``dens``, ``pres``, ``temp``, ``sphu`` and
``zsfc`` at each particle, and no other column of this table.
:doc:`flexpart` lists them and where they differ from HYSPLIT's.

Columns runtraj adds
--------------------

``release_height``
   Release height of each particle, in meters in the release's own
   vertical reference, for column and multipoint releases
   (:func:`add_release_heights`). For a column it is the center of the
   particle's slab, the part of the column the particle stands for.

``time``
   The time of each row, in UTC: the release time plus ``age``. It is
   not stored; reading a particle file adds it, and so does
   :func:`runtraj.run_trajectories`.

``weight``
   How much each row counts, written by the particle transforms
   (:doc:`transforms`), which multiply it; 1 where none has run. It is not
   stored: a stage applies its transforms when it reads the particles, and
   multiplies what it makes from a row by the row's weight. Pressure
   weighting and an averaging kernel also add ``pwf``, ``xpres`` and
   ``ak_weight``.

Checking a table
----------------

.. autosummary::
   :toctree: _api
   :nosignatures:

   PARTICLE_SCHEMA
   check_particles

A result file's release
-----------------------

A stage that writes a Parquet file of its own records the release in the
file's metadata with :func:`~runtraj.particles.release_metadata`, and
:func:`~runtraj.particles.stored_release` reads it back from the file's
schema metadata. Neither names the metadata key, which is runtraj's.

.. autosummary::
   :toctree: _api
   :nosignatures:

   release_metadata
   stored_release

After a run
-----------

After a run runtraj adds the release heights to any model's particles.
That, with the columns the model wrote, is what a particle file stores and
``sim.particles`` returns: a model that writes no ``foot`` has none there.

.. autosummary::
   :toctree: _api
   :nosignatures:

   add_release_heights

Met columns for a model that writes only positions
--------------------------------------------------

.. currentmodule:: runtraj.sampling

A transport model that writes only positions leaves out the columns
HYSPLIT writes from the met. :func:`sample_met` reads ``mlht``, ``dens``,
``pres`` and ``zsfc`` from the met files at each particle's position and
time, as HYSPLIT reads them. Give it the files the run read and the top of
its domain, which a stored run records:

.. code-block:: python

   import runtraj
   from runtraj.sampling import sample_met

   meta = runtraj.particles.particles_metadata(sim.particles_path)
   sampled = sample_met(
       sim.particles.drop(columns=["mlht", "dens"]),
       meta.met_files,
       columns=["mlht", "dens"],
       z_top=meta.settings["z_top"],
   )

The sampled values differ a little from HYSPLIT's own columns, by the
amounts under :func:`sample_met`'s Notes. ``mlht`` from :func:`sample_met`
is the met's ``PBLH``. A transport model may find the mixed layer from
the met's profile instead: HYSPLIT does by default (``kmixd: 3``), and
FLEXPART does on ERA5. :func:`calc_pbl_height` finds the mixed layer the
way the run did; give it the run's ``method``:

.. code-block:: python

   from runtraj.sampling import calc_pbl_height

   mlht = calc_pbl_height(sim.particles, meta.met_files)  # HYSPLIT, kmixd: 3
   mlht = calc_pbl_height(sim.particles, meta.met_files, method="hysplit_temperature")  # kmixd: 1
   mlht = calc_pbl_height(particles, met, method="flexpart")  # FLEXPART on ERA5

Met files that are not ARL files, such as GRIB, are read as one Dataset in
:func:`arlmet.open_dataset`'s layout, the way a converter in
:mod:`arlmet.converters` writes it. Both functions take that Dataset in
place of the files, and sample it as they would sample the ARL file
:func:`arlmet.write_dataset` would write from it. The met of a transport
model that reads such files opens them for you: a variant's
``met_config.open(files, bounds)`` (:meth:`runtraj.models.MetConfig.open`)
returns the Dataset, cropped to where the particles are, or the paths of
ARL files as they are.

.. autosummary::
   :toctree: _api
   :nosignatures:

   sample_met
   calc_pbl_height

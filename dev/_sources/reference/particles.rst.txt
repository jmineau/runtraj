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
release (:func:`add_release_heights`). HYSPLIT 5.1.0 writes no such row: its
first row is one time step after release. For it, runtraj matches each
particle to a release point from its first position, and takes a column's
particles to be released bottom to top in ``particle`` order. A HYSPLIT
build that writes the release rows (``directory``) makes the matching exact.

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
     - Terrain height above sea level. It is not in the default
       ``varsiwant``.
   * - ``zcfx``
     - m
     - Vertical displacement by convective flux along the trajectory,
       summed over the steps (the Grell scheme, ``capemin: -2``). HYSPLIT
       reads this name only: ``zfx1`` stops it, and the config refuses it.

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

Checking a table
----------------

.. autosummary::
   :toctree: _api
   :nosignatures:

   PARTICLE_SCHEMA
   check_particles

After a run
-----------

After a run runtraj adds the release heights to any model's particles.
That, with the columns the model wrote, is what a particle file stores and
``sim.particles`` returns: a model that writes no ``foot`` has none there.

.. autosummary::
   :toctree: _api
   :nosignatures:

   add_release_heights

Core Objects
============

.. currentmodule:: runtraj

The project, its releases, the simulations it runs, and their particles.

One release, without a project
-------------------------------

:func:`run_trajectories` runs the transport model for one release and
returns its particles. A project runs the same step for each of its
releases and stores the results.

.. code-block:: python

   particles = runtraj.run_trajectories(release, met, n_hours=-24, numpar=200)

.. autosummary::
   :toctree: _api
   :nosignatures:

   run_trajectories

Project interface
-----------------

:class:`Project` is what you open a project with. It runs the transport
model, and each stage the project lists, and reads the results.

.. autosummary::
   :toctree: _api
   :nosignatures:

   Project

Release objects
----------------

.. autosummary::
   :toctree: _api
   :nosignatures:

   Release
   PointRelease
   ColumnRelease
   MultiPointRelease
   read_releases
   releases.write_releases
   releases.releases_to_frame
   releases.releases_from_frame
   releases.parse_release_id

Simulation objects
------------------

A :class:`~runtraj.config.Variant` is one variant of a project, resolved: its configs, its
met, the transport model build, and the hashes that find its results.
``project.variants`` holds them, and :meth:`ProjectConfig.resolve` makes
them from a config. :class:`Simulation` is one release under one variant,
which ``project.simulation(release_id, variant)`` returns.

.. autosummary::
   :toctree: _api
   :nosignatures:

   Simulation
   config.Variant

Settings and their hashes
-------------------------

:mod:`runtraj.identity` records what a run was made with, as its folder's
``_settings.yaml`` holds it, and hashes it. A stage records its own settings
(:meth:`runtraj.stages.Stage.settings`), which are hashed with the
particles' hash and the hash of each stage it reads. A stored record is
read back through the current config classes, so a setting added since, with
a default, still matches.

.. autosummary::
   :toctree: _api
   :nosignatures:

   identity.run_settings
   identity.read_run_settings
   identity.transport_from_settings
   identity.stage_hash
   identity.settings_hash
   identity.file_stamp
   models.ModelInfo

Particles
---------

A simulation's particles are a pandas DataFrame. runtraj's methods on them
are under ``.runtraj`` (:class:`~runtraj.particles.ParticlesAccessor`). The
functions below read and write particle files without a project.

.. autosummary::
   :toctree: _api
   :nosignatures:

   read_particles
   particles.particles_metadata
   particles.write_particles
   particles.ParticlesAccessor

Simulation tables
-----------------

``project.releases`` is a pandas DataFrame with one row per release, and
``project.simulations`` one with one row per simulation. Select rows as in
pandas, then pass the selection to the project's :meth:`~Project.status`,
:meth:`~Project.incomplete`, or :meth:`~Project.particles`.

.. code-block:: python

   sims = project.simulations
   wbb = sims[(sims.variant == "hrrr") & (sims.site == "WBB")]
   particles = project.particles(wbb)

Boxes
-----

A :class:`Bounds` is documented under :doc:`configuration`.
:mod:`runtraj.spatial` has the CRS helpers.

.. autosummary::
   :toctree: _api
   :nosignatures:

   spatial.same_crs
   spatial.is_longlat
   spatial.horizontal_dims

Configuration
=============

.. currentmodule:: runtraj

``config.yaml`` is blocks, each a field of :class:`ProjectConfig`:

.. code-block:: yaml

   mets:
     hrrr: /path/to/hrrr_arl
   models:
     hysplit: {n_hours: -24, numpar: 200}
   variants:
     hrrr: {}
     hrrr-long: {n_hours: -72}
   execution:
     backend: local
   output: output

``mets`` names the meteorology (for HYSPLIT,
:class:`~runtraj.models.hysplit.MetConfig`). ``models`` names the transport
models, each block with that model's settings
(:class:`~runtraj.models.hysplit.HysplitConfig` for ``hysplit``).
``stages`` names what is made from each simulation's particles after the
model runs, each block with the import path of its stage's class and then
the stage's settings. A stage's block is named for its stage, which is also
the name of its folder in the output directory and of its column in
``project.status()``. Without a ``stages`` block, a project makes particles
only.
``variants`` says what runs, and ``execution`` where
(:class:`ExecutionConfig`). Each class lives next to the code that uses it
(``runtraj.execution.config``, ``runtraj.models.hysplit``).

No setting sits at the top level. One written there is refused with a
message that names its block, such as ``'n_hours' is a hysplit setting.
Write it under models: hysplit:.`` A key may be written once in each
block: YAML would keep the last of two, so runtraj refuses the file and
names the line of the second.
Each page below lists its fields with their defaults and is generated from
the code.

Every key
---------

These tables are generated from the code, so they always match the
installed version.

Top level
~~~~~~~~~

The blocks, and where the results go.

.. config-model:: runtraj.config.ProjectConfig
   :declared-only:

HYSPLIT
~~~~~~~

The keys of the ``hysplit`` block under ``models:``, the transport model's
settings (:doc:`hysplit`). They are the defaults of every variant that runs
HYSPLIT, and a variant may override any of them by writing the key itself.
Every key but ``n_hours`` has the default below, so a block may be as
short as ``hysplit: {n_hours: -24}``; ``n_hours`` has no default, and a
config without it is refused. ``capemin``, ``kblt``, ``kmixd``,
``krand`` and ``vscales`` are unset unless given: HYSPLIT chooses them when
it runs.

.. config-model:: runtraj.models.hysplit.HysplitConfig

A model in another package gets a block of any name that gives the model
class's import path as ``class``, then that model's own settings:

.. code-block:: yaml

   models:
     hysplit: {n_hours: -24}
     mine: {class: mypkg.models.MyModel, n_hours: -24}
   variants:
     hrrr: {model: hysplit}
     hrrr-mine: {model: mine, met: hrrr}

With two or more models, each variant names its own with ``model``. A model
has one block: a second set of settings for it is a second variant.

A stage
~~~~~~~

A stage's block under ``stages:`` gives the import path of the stage's class
as ``class``, then that stage's own settings, which are the defaults of
every variant. A variant changes some under its own ``stages`` key, under
the stage's name, and sets the stage to null to go without it. PYSTILT's
footprint is such a stage:

.. code-block:: yaml

   stages:
     footprint:
       class: stilt.footprint.FootprintStage
       grid: {xmin: -113.0, xmax: -110.5, ymin: 40.0, ymax: 42.0, xres: 0.01, yres: 0.01}
   variants:
     hrrr: {}
     hrrr-coarse: {stages: {footprint: {grid: {xres: 0.05, yres: 0.05}}}}
     hrrr-particles: {stages: {footprint: null}}

A met
~~~~~

The keys of one entry under ``mets:``, for HYSPLIT
(:doc:`../guides/meteorology`).

.. config-model:: runtraj.models.hysplit.MetConfig

Execution
~~~~~~~~~

The keys under ``execution:`` (:doc:`../guides/running`). ``backend`` names
where the project runs, and every setting of that place is in the block of
its name. Only that block is read, so a config can hold both.

.. config-model:: runtraj.execution.config.ExecutionConfig

The keys under ``execution: local:``, a run on this machine.

.. config-model:: runtraj.execution.config.LocalConfig

The keys under ``execution: slurm:``, a Slurm job array
(:doc:`../guides/slurm`). Any other key is an ``sbatch`` option by its name.

.. config-model:: runtraj.execution.config.SlurmConfig

The keys under ``execution: slurm: fetch:``, the Slurm resources of the job
that gets the met files before a job array (:ref:`slurm-fetch`).

.. config-model:: runtraj.execution.config.FetchConfig

Grids
~~~~~

A :class:`Grid` is a rectilinear raster: a met's crop box is a
:class:`Bounds`, and a stage that sums particles onto cells takes a
:class:`Grid`. Longitudes count modulo 360, so a grid across 180° can be
written ``xmin: 170``, ``xmax: 190`` or ``xmin: 170``, ``xmax: -170``. An
``xmax`` west of ``xmin`` is read as crossing 180° when the two are on
either side of 0° or one is written past 180°: with both in one hemisphere
(``xmin: -110.5``, ``xmax: -113``) they are taken to be swapped and the grid
is refused. A grid spans at most 360°. Projected grids (a ``crs`` such as
``EPSG:32612``) keep their bounds in degrees and take ``xres`` and ``yres``
in the projection's units. A grid of more than a billion cells is refused,
which catches a resolution in degrees on a grid in meters.

Config objects
--------------

A config is what you write. ``project.variants`` turns each declared
variant into a :class:`runtraj.config.Variant`: its transport settings
checked by the model's config class, and what the file alone does not
say, such as the transport model build. A variant with ``realizations: N``
stays one variant.

The ``RUNTRAJ_COMPUTE_ROOT`` environment variable sets the compute root,
where each run's workdir is made. It never changes a result, and projects
can share one.

.. autosummary::
   :toctree: _api
   :nosignatures:

   ProjectConfig
   ExecutionConfig
   execution.LocalConfig
   execution.SlurmConfig
   execution.FetchConfig
   Bounds
   Grid

Parameters
----------

The settings that shape a run's particles belong to the transport model,
and are written in its block under ``models:`` (``hysplit`` when the config
has no ``models``). HYSPLIT's are in
:class:`runtraj.models.hysplit.HysplitConfig`, under HYSPLIT's names (see
:doc:`hysplit`). Each model's block holds all of its settings, the ones
runtraj reads itself among them (``n_hours`` and ``seed``): nothing passes
from one model's block to another's. What a stage does with the particles
afterwards is in the stage's block, and does not change the particles or
their settings folder.

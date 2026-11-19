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

A config runs code
------------------

A ``class`` in a model's or a stage's block, and a transform's ``kind``,
name a Python object by its import path. runtraj imports that module and
calls the object, which is how a project uses a model or a stage from any
installed package. runtraj does not check what the path names: any
installed module is imported and any callable is called, even by
``runtraj check``. So treat a ``config.yaml`` as you would a Python script,
and run one only if you would run a script from the same person.

A service that runs configs other people send should not pass their YAML to
runtraj. Build the :class:`ProjectConfig` yourself from the fields you
allow, with the import paths you choose.

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

The keys of one entry under ``pollutants:``, in ``mode: concentration``
(:doc:`../guides/concentration`).

.. config-model:: runtraj.models.hysplit.Pollutant

The keys of one entry under ``grids:``, in ``mode: concentration``.

.. config-model:: runtraj.models.hysplit.ConcentrationGrid

The keys of one entry under ``conversions:``, with ``ichem: 2``.

.. config-model:: runtraj.models.hysplit.Conversion

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

FLEXPART
~~~~~~~~

The keys of the ``flexpart`` block, for a FLEXPART 11 checkout you built.
They keep FLEXPART's own names from its ``COMMAND`` file; runtraj writes the
rest of that file, and FLEXPART's other input files, for each release.

.. config-model:: runtraj.models.flexpart.FlexpartConfig

FLEXPART reads GRIB files, not ARL: a met for it is a directory of GFS or
ERA5 files, found by their valid times.

.. config-model:: runtraj.models.flexpart.FlexpartMetConfig

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
its name. Only that block is read, so a config can hold several.

.. config-model:: runtraj.execution.config.ExecutionConfig

The keys under ``execution: local:``, a run on this machine.

.. config-model:: runtraj.execution.config.LocalConfig

The keys under ``execution: slurm:``, a Slurm job array
(:doc:`../guides/slurm`). Any other key is an ``sbatch`` option by its name.

.. config-model:: runtraj.execution.config.SlurmConfig

The keys under ``execution: slurm: fetch:``, the Slurm resources of the job
that gets the met files before a job array (:ref:`slurm-fetch`).

.. config-model:: runtraj.execution.config.FetchConfig

The keys under ``execution: kubernetes:``, a Kubernetes indexed Job
(:ref:`kubernetes-submit`), in Kubernetes' own names and units.

.. config-model:: runtraj.execution.config.KubernetesConfig

The keys of one entry under ``execution: kubernetes: volumes:``, a
PersistentVolumeClaim mounted in every pod.

.. config-model:: runtraj.execution.config.Volume

Boxes
~~~~~

A :class:`Bounds` is a longitude/latitude box, in degrees, such as a met's
crop box. Longitudes count modulo 360, so a box across 180° can be written
``xmin: 170``, ``xmax: 190`` or ``xmin: 170``, ``xmax: -170``, and a box
360° wide or more holds every longitude (:meth:`Bounds.contains`).

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
   execution.KubernetesConfig
   execution.Volume
   Bounds

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

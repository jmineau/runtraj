Architecture
============

This page says how runtraj is put together: what a project is, how a run
goes from a project to result files, and how runtraj knows which work is
finished. ``AGENTS.md`` at the repository root has the same in brief, with
the rules a change must keep.

The design in brief
-------------------

**There is no index, manifest, or registry.** A project is a directory of
inputs (:class:`runtraj.Project`: ``config.yaml`` and ``releases.csv``) and
an output directory (:class:`runtraj.output.Output`) that ``config.yaml``
names and several projects can share. Its simulations are its releases
crossed with its variants. Each :class:`~runtraj.config.Variant` has run
settings, whose hash names its particles folder, and the settings of each
stage it uses, hashed with what the stage reads (:mod:`runtraj.identity`).
Variants with equal run settings share one run per release, and a changed
setting hashes to a new folder (:doc:`../reference/layout`).

**A project reads; the workers write results.** A project writes only its
inputs and knows no scheduler: :func:`runtraj.execution.run` and
:func:`runtraj.execution.submit` start the workers. The tables a project
gives (``project.releases``, ``project.simulations``) are plain pandas
DataFrames, and a selection of rows goes back to the project's methods
(``status``, ``incomplete``, ``particles``).

**A transport model and a stage are classes named by import path.**
``config.yaml`` names a model in another package, and each stage, by the
import path of its class, so the config, the settings records, the output
directory and the worker reach them without importing them by name. A
model subclasses :class:`runtraj.models.TransportModel`; a stage, which
makes one file per release after the model runs, subclasses
:class:`runtraj.stages.Stage`. Each base class implements every optional
member with its default, so runtraj calls them directly and a plug-in
overrides only what it does differently. HYSPLIT and FLEXPART are the
models runtraj supports, and each one's package (:mod:`runtraj.models.hysplit`,
:mod:`runtraj.models.flexpart`) is the only code that knows that model's
files.

Module map
----------

.. code-block:: text

   src/runtraj/
     cli.py          the runtraj command
     project.py      Project: inputs, the simulations table, run and submit, status
     check.py        what would stop a project's runs, from its inputs
     fetch.py        gets the met files the unfinished simulations need
     output.py       Output: the output directory; completed, the completion rule
     simulation.py   Simulation: a frozen (release, variant, output) value
     config.py       ProjectConfig and Variant
     identity.py     settings records and hashes
     stages.py       the Stage base class, get_stage, check_stage
     testing.py      run_stage: a stage end to end in a project, on the replay model; check_model
     releases/       release classes, releases.csv, shared validation
     particles/      the particle table and files, release heights, particles.runtraj
     models/         base (the TransportModel and TransportConfig base classes), registry (MODELS, get_model), run (run_model)
       hysplit/      config, met, driver, CONTROL and SETUP.CFG writers, failures
     execution/      ExecutionConfig, the runner (plan, local run), slurm (the job array, whose script requeues a stopped task), pbs (the job array), the worker,
                     kubernetes (the indexed Job, the hourly CronJob), inputs (copies to a bucket, the lock), schedule
     transforms/     particle transforms, each multiplying the weight column
     sampling.py     sample_field, sample_met, calc_pbl_height: fields and met read at points
     meteorology.py, spatial.py, visualization.py, exceptions.py

The import contracts in ``pyproject.toml`` (``just imports``) hold this
shape: the modules are in layers, only :mod:`runtraj.models` imports a
transport model's package (such as HYSPLIT's, ``runtraj.models.hysplit``),
no model's package imports another's, and the project config reads no
results and runs nothing.

How a run goes
--------------

A run (``runtraj run``, :meth:`runtraj.Project.run`,
:meth:`runtraj.Project.submit`) happens in this process or as a Slurm job
array whose tasks each run ``runtraj run --task i/N``. The unit of work is a
release: the worker runs the model once per run-settings hash, then each
missing stage file of the variants that share the particles, a stage after
the ones it reads. A Slurm task rebuilds the project in another process, so
anything a worker needs must be in the project or the output directory.

How runtraj tracks finished work
--------------------------------

**Completion is by file.** A simulation is complete when the model's output
file exists, and so does the file of every stage its variant uses.
:func:`runtraj.output.completed` is the one rule, read from folder listings.
A failure record (``<release id>.failure.yaml`` in the logs) is a note,
removed when the result is written and never read by completion.

**Identity is content.** A results folder is named from the settings that
made it, so runtraj never deletes or overwrites one: a changed setting makes
a new folder beside the old.

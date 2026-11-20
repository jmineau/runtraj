Execution
=========

Most users only need ``project.run()``, ``project.submit()``, or
``runtraj run`` (:doc:`../guides/running`). The functions below do the
work underneath.

- :func:`~runtraj.execution.run` is what ``project.run()`` calls. It finds the
  releases with missing results, runs them, and waits until they finish.
  ``task=(i, n)`` runs share ``i`` of ``n`` here (:func:`~runtraj.execution.task_share`),
  which is what ``runtraj run --task i/n`` does.
- :func:`~runtraj.execution.submit` is what ``project.submit()`` calls. It
  writes a job array script (:func:`~runtraj.execution.job_script`) whose
  tasks each run ``runtraj run --task``, submits it with ``sbatch``, and
  returns the job id at once. With ``backend: kubernetes`` it makes an
  indexed Job instead (:func:`~runtraj.execution.kubernetes.job_manifest`)
  and creates it with ``kubectl``.
- :func:`~runtraj.execution.worker.run_particles` runs the transport model for one
  :class:`~runtraj.Simulation` and writes its particles and log.
- :func:`~runtraj.execution.worker.run_stages` runs the stages of the variants
  that share those particles: each stage file
  that is missing, in the order the stages read one another, and each
  again when what it reads was made again.
  :func:`~runtraj.execution.worker.run_stage` runs one, which writes its own
  file.
- :func:`~runtraj.execution.worker.run_release` runs every variant of one release,
  skipping what exists. Variants that share particles run the transport model once and
  run their stages on the particles in memory. A failure is recorded
  with the simulation (:attr:`runtraj.Simulation.failure`). Workers are
  always handed releases.
- :func:`~runtraj.execution.worker.run_releases` runs a list of releases, in this
  process or in a process pool.

On Slurm each task of the job array runs one share of the releases
(:func:`~runtraj.execution.task_share`) with ``runtraj run --task``. A local
run happens in this process. A program that starts the tasks itself, on
another scheduler, writes :func:`~runtraj.execution.pending` to the
``releases.txt`` each task reads (``runtraj run --releases``);
:func:`~runtraj.execution.wait` waits for a
Slurm job array by its id.

Running
-------

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.execution.run
   runtraj.execution.submit
   runtraj.execution.pending
   runtraj.execution.wait
   runtraj.execution.task_share

Workers
-------

Internal: importable from their modules, and free to change.

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.execution.runner.resolve_compute_root
   runtraj.execution.worker.run_particles
   runtraj.execution.worker.run_stage
   runtraj.execution.worker.run_stages
   runtraj.execution.worker.run_release
   runtraj.execution.worker.run_releases

Slurm job arrays
----------------

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.execution.job_script
   runtraj.execution.fetch_script

Kubernetes indexed Jobs
-----------------------

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.execution.kubernetes.job_manifest
   runtraj.execution.kubernetes.submit_job
   runtraj.execution.inputs.push
   runtraj.execution.inputs.pull
   runtraj.execution.inputs.hash_inputs

Transport model
----------------

The worker runs a simulation through the transport model its settings name.
HYSPLIT is the one built in, and CONTRIBUTING describes how to add
another, named by its import path.
A model fails a run that its meteorology does not cover, as
``MET_COVERAGE``. Particles that all leave the met's domain before the end
of the run are a complete run. Use :func:`~runtraj.models.check_model` in
the tests of a model of your own.

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.models.TransportModel
   runtraj.models.BatchedTransportModel
   runtraj.models.TransportConfig
   runtraj.models.ModelRun
   runtraj.models.get_model
   runtraj.models.run_model
   runtraj.models.run_model_many
   runtraj.models.open_met
   runtraj.models.check_model
   runtraj.models.hysplit.HysplitModel

Stages
------

A stage makes one file per release from a simulation's results, after the
transport model runs. PYSTILT's footprint is one
(``stilt.footprint.FootprintStage``). A ``stages`` block of
``config.yaml`` names its stage's class by import path, so a stage in a
package of your own needs no registration, and CONTRIBUTING describes how to
write one. Use :func:`~runtraj.stages.check_stage` in its tests.

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.stages.Stage
   runtraj.stages.get_stage
   runtraj.stages.ordered
   runtraj.stages.check_stage

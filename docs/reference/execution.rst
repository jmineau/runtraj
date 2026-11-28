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
- :func:`~runtraj.execution.kubernetes.schedule_job` is what ``runtraj
  schedule`` calls: a CronJob (:func:`~runtraj.execution.kubernetes.cron_job_manifest`)
  whose pod each hour runs ``runtraj tick``, which adds the hours now due
  (:func:`~runtraj.execution.schedule.add_due_releases`) and runs them.
- :func:`~runtraj.execution.worker.run_result` runs the transport model for one
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
``releases.txt`` each task reads (``runtraj run --releases``;
:doc:`../guides/any_scheduler`); :func:`~runtraj.execution.wait` waits
for a Slurm job array by its id.

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
   runtraj.execution.worker.run_result
   runtraj.execution.worker.run_stage
   runtraj.execution.worker.run_stages
   runtraj.execution.worker.run_release
   runtraj.execution.worker.run_releases

.. _execution-backends:

Backends
--------

Where the releases run is a backend, which ``execution.backend`` names:
``local`` runs them in this process, ``slurm`` as one Slurm job array,
``pbs`` as one PBS job array, ``kubernetes`` as one Kubernetes indexed Job.
Each is a
:class:`~runtraj.execution.Backend`, and its block under ``execution`` is
its ``config_class``, a :class:`~runtraj.execution.BackendConfig`.
:func:`~runtraj.execution.run` and :func:`~runtraj.execution.submit` find
it with :func:`~runtraj.execution.get_backend` and hand it the releases
whose results are missing.

A scheduler runtraj does not have can be a backend in a package of your
own, named by the import path of its class, as a transport model or a
stage is:

.. code-block:: yaml

   execution:
     backend: mypkg.backends.QueueBackend
     myqueue:            # the class's name
       queue: short
       cpus: 4

Its :meth:`~runtraj.execution.Backend.submit` starts the tasks, each of
which runs ``runtraj run <project> --task i/N`` with ``i`` from the
scheduler (``$SLURM_ARRAY_TASK_ID`` in a Slurm array), and returns what
names them. A backend that sets ``waits`` and writes
:meth:`~runtraj.execution.Backend.run` waits for them, so ``runtraj run``
can use it; one that writes :meth:`~runtraj.execution.Backend.jobs` lists
them in ``runtraj status``. The tasks need nothing else: every task splits
the releases the same way (:func:`~runtraj.execution.pending`), skips
what is complete, and exits with a code that says whether to try it again
(:ref:`exit-codes`).

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.execution.Backend
   runtraj.execution.BackendConfig
   runtraj.execution.get_backend
   runtraj.execution.runner.LocalBackend
   runtraj.execution.slurm.SlurmBackend
   runtraj.execution.pbs.PbsBackend
   runtraj.execution.kubernetes.KubernetesBackend

Slurm job arrays
----------------

The Slurm backend is :mod:`runtraj.execution.slurm`: it writes the job
array and fetch scripts, submits them with ``sbatch``, follows the tasks
with ``sacct`` (``squeue`` where it does not answer). The job array script requeues a task that Slurm stops
near its time limit or preempts; the worker only stops, and exits with 3.

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.execution.job_script
   runtraj.execution.fetch_script
   runtraj.execution.slurm.submit_array

PBS job arrays
--------------

The PBS backend is :mod:`runtraj.execution.pbs`: it writes the job array
script, submits it with ``qsub``, and follows it with ``qstat``.

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.execution.pbs.job_script
   runtraj.execution.pbs.submit_array
   runtraj.execution.pbs.wait
   runtraj.execution.pbs.max_array_size
   runtraj.execution.runner.write_submission
   runtraj.execution.runner.array_size

Kubernetes indexed Jobs
-----------------------

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.execution.kubernetes.job_manifest
   runtraj.execution.kubernetes.submit_job
   runtraj.execution.kubernetes.project_jobs
   runtraj.execution.kubernetes.job_summary
   runtraj.execution.runner.fetch_shared
   runtraj.execution.inputs.push
   runtraj.execution.inputs.pull
   runtraj.execution.inputs.hash_inputs

Running every hour
------------------

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.execution.kubernetes.schedule_job
   runtraj.execution.kubernetes.cron_job_manifest
   runtraj.execution.kubernetes.cron_job_name
   runtraj.execution.schedule.add_due_releases
   runtraj.execution.schedule.due_hours
   runtraj.execution.schedule.read_sites
   runtraj.execution.schedule.hourly_releases
   runtraj.execution.schedule.check_now
   runtraj.execution.inputs.lock
   runtraj.execution.inputs.get
   runtraj.execution.inputs.put

Transport model
----------------

The worker runs a simulation through the transport model its settings name.
runtraj supports HYSPLIT 5.0 and later (:doc:`hysplit`) and FLEXPART 11 (:doc:`flexpart`),
and has no default: ``config.yaml`` names the model in its
``models`` block. CONTRIBUTING describes how to add another, named by its import path.
A model fails a run that its meteorology does not cover, as
``MET_COVERAGE``. Particles that all leave the met's domain before the end
of the run are a complete run. Use :func:`~runtraj.testing.check_model` in
the tests of a model of your own.

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.models.TransportModel
   runtraj.models.TransportConfig
   runtraj.models.ModelRun
   runtraj.models.get_model
   runtraj.models.run_model
   runtraj.models.MetConfig
   runtraj.testing.check_model
   runtraj.models.hysplit.HysplitModel
   runtraj.models.flexpart.FlexpartModel
   runtraj.models.flexpart.read_partoutput

Stages
------

A stage makes one file per release from a simulation's results, after the
transport model runs. PYSTILT's footprint is one
(``stilt.footprint.FootprintStage``). A ``stages`` block of
``config.yaml`` names its stage's class by import path, so a stage in a
package of your own needs no registration, and CONTRIBUTING describes how to
write one. In its tests, :func:`~runtraj.stages.check_stage` checks its
settings record, and :func:`~runtraj.testing.run_stage` runs it end to end
in a project of one release, on particles you give it and with no transport
model installed (:class:`~runtraj.testing.ReplayModel`).

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.stages.Stage
   runtraj.stages.get_stage
   runtraj.stages.ordered
   runtraj.stages.check_stage
   runtraj.testing.run_stage
   runtraj.testing.ReplayModel

On A PBS Cluster
================

PBS Pro and OpenPBS clusters, such as NCAR's Derecho and Casper, run a
project the way a Slurm cluster does (:doc:`slurm`): runtraj splits the
unfinished releases among the subjobs of one job array and submits it with
``qsub``. You don't write a PBS script yourself.

Your project folder, its output directory, and the Python environment must
be on a filesystem the compute nodes can see.

Set it up
---------

Add an ``execution`` section with a ``pbs`` block to ``config.yaml``:

.. code-block:: yaml

   execution:
     backend: pbs
     pbs:
       n_workers: 50               # subjobs running at once
       walltime: "02:00:00"        # time limit per subjob
       queue: main
       account: UABC0001
       mem: 8gb

Then check the project and submit it from the environment runtraj is
installed in:

.. code-block:: bash

   runtraj check ./my_project
   runtraj submit ./my_project

``runtraj submit`` prints the job ID, such as ``1234[].desched1``, and
returns. ``runtraj run`` submits it too, then waits until it finishes,
asking ``qstat`` every 30 seconds. ``qstat -t <job ID>`` shows the
subjobs. Each subjob runs with the same Python that submitted it, and with
the environment of the shell that submitted it (``qsub -V``).

Options
-------

The keys of the ``pbs`` block:

``n_workers``, ``n_tasks``
   As on Slurm: ``n_tasks`` subjobs share the releases, ten per worker
   unless you set it, and at most ``n_workers`` run at once
   (``-J 0-499%50``). There is never more than one subjob per release, nor
   more than the server's ``max_array_size`` (``qstat -Bf``).

``cpus``
   Releases each subjob runs at once, and the CPUs (``ncpus``) it asks for.

``walltime``
   The time limit of each subjob, as ``HH:MM:SS`` or seconds. It is
   required.

``mem``, ``queue``, ``account``
   ``mem`` goes into the subjob's chunk, ``select=1:ncpus=<cpus>:mem=<mem>``;
   ``queue`` is ``-q`` and ``account`` is ``-A``.

``setup``
   Shell commands to run at the start of each subjob, before runtraj, such
   as ``module load`` lines.

Any other key
   A PBS resource by its name, written as ``#PBS -l <key>=<value>``, such as
   ``place: scatter``. ``select`` replaces the chunk runtraj writes, for a
   chunk that names more, such as ``select: "1:ncpus=4:ngpus=1"``.

Each submission is a folder ``_pbs/<date_time>_<id>/`` in the project:
``job.sh`` is the script given to ``qsub``, ``job.id`` its job ID, and
``<i>.log`` the log of subjob ``i``.

What PBS does not do here
-------------------------

- **No requeue at the time limit.** PBS sends no warning before
  ``walltime``, and a user cannot put a subjob back in the queue, so a
  subjob that runs out of time stays stopped. Run the project again: it
  skips what finished. A subjob that PBS preempts is put back by PBS
  itself, since the job is rerunnable (``-r y``).
- **No fetch job.** Each subjob downloads the met files its releases lack.
  When many subjobs share files, get them first with ``runtraj fetch
  ./my_project``.

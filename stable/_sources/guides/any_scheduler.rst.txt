On Any Scheduler
================

runtraj submits a project to Slurm (:doc:`slurm`), PBS (:doc:`pbs`) and
Kubernetes (:doc:`containers`) itself. Any other scheduler that can start
one command many times, each with its own number, can run a project too:
LSF, HTCondor, a cloud batch service, or GNU parallel on one big machine.
Each start runs one task, and the tasks need nothing from each other.

The task
--------

Every task runs the same command with its own number ``I``, from 0 to
``N - 1``:

.. code-block:: bash

   runtraj run ./my_project --task I/N

Task ``I`` runs every ``N``-th release of the project, starting at
``I``, with the releases put in the order of a hash of their ids: with
``N = 4``, task 1 runs the 2nd, 6th, 10th, and so on, of that order. The
order is unrelated to ``releases.csv``'s, so the releases left to run
spread over the tasks even when they lie together in the file, such as
one site's hours among many sites'. Every task splits the releases the
same way, whether they are finished or not, and then skips those whose
results all exist.
``--cpus`` sets how many of its releases a task runs at once (1 unless the
``local`` block of ``execution`` says otherwise). The task runs here, in
this process, whatever ``execution.backend`` says.

``--releases ids.txt`` runs only the releases listed in a file, one id per
line, in its order; with ``--task`` too, each task takes its share of that
list. A driver that hands out releases itself, from a queue of its own,
gives each start a file of its own and ``--task 0/1``, so that it runs all
of them here.

What a task relies on
---------------------

- **Completion is by file.** A simulation is complete when its result
  file, and the file of each stage its variant uses, exist in the output
  directory. Nothing else records what is done, so the tasks need no
  database and do not talk to each other.
- **Tasks run in any order, and at the same time.** Their shares do not
  overlap, and each run gets a new workdir in the compute root
  (``--compute-root``, else ``RUNTRAJ_COMPUTE_ROOT``, else under
  ``$TMPDIR``), so tasks on one machine can share it.
- **A task run again fills in what is missing.** After a task is stopped,
  preempted or runs out of time, start the same command again: it skips
  what its first start finished. A whole project run again, the same way,
  runs only what no task finished.
- **Every task sees the same releases.** Give every task the same ``N``,
  and do not add releases to ``releases.csv`` while the tasks run, or give
  them all one ``--releases`` file, since a new release moves the shares.
  ``runtraj submit`` gives its tasks such a file for this reason.
- **Every task sees the same files**: the project folder, the met files
  and the output directory, on a filesystem every machine mounts (or the
  output on an object store), and the same ``hycs_std``, whose checksum is
  part of each run's settings folder.

A met that downloads its files gets them in each task, so tasks whose
releases share a day download the same file at once. Run ``runtraj fetch
./my_project`` first, once, to get them all (:doc:`running`).

``runtraj status ./my_project`` counts what is finished, failed and left,
and ``runtraj status ./my_project --json`` gives the same counts to a
program.

GNU parallel
------------

On one machine with many cores, run 32 tasks, 8 at a time:

.. code-block:: bash

   runtraj check ./my_project
   parallel -j 8 runtraj run ./my_project --task {}/32 ::: $(seq 0 31)
   runtraj status ./my_project

If some tasks exit with 3 (stopped, or a connection error), run the same
``parallel`` line again: only what did not finish runs.

LSF
---

A job array of 100 tasks, 20 at a time, submitted with ``bsub < job.sh``.
LSF numbers the tasks from 1, so the script subtracts 1. At the run limit
(``-W``) LSF sends ``SIGUSR2``, which runtraj does not stop on, so the
script passes it on as the ``SIGTERM`` it does stop on; the task then exits
with 3, and ``-Q "3"`` puts it back in the queue to finish its share. The
``logs`` folder must exist before you submit:

.. code-block:: bash

   #!/bin/bash
   #BSUB -J "runtraj[1-100]%20"
   #BSUB -n 1
   #BSUB -W 2:00
   #BSUB -o logs/%I.log
   #BSUB -Q "3"
   runtraj run /path/to/my_project --task $((LSB_JOBINDEX - 1))/100 &
   pid=$!
   trap 'kill -TERM "$pid"' USR2 TERM INT
   wait "$pid"; code=$?
   # A trapped signal ends the first wait early; the second gets the exit code.
   [ "$code" -gt 128 ] && { wait "$pid"; code=$?; }
   exit "$code"

A scheduler you use often
-------------------------

To make ``runtraj submit`` and ``runtraj run`` start the tasks themselves,
write a backend: a class that subclasses ``runtraj.execution.Backend``, in
a package of your own, named in ``execution.backend`` by its import path
(:ref:`the reference <execution-backends>`).

.. _exit-codes:

Exit codes
----------

``runtraj run`` exits with:

.. list-table::
   :header-rows: 1
   :widths: 10 90

   * - Code
     - Meaning
   * - 0
     - Every simulation it ran is complete.
   * - 1
     - Some simulations failed. ``runtraj status`` counts them by reason. A
       retry runs them again, which helps only when the cause is fixed.
   * - 2
     - The command line was wrong: an unknown option, a bad value, or a
       directory that is not a project. Or the project's files are invalid:
       ``config.yaml`` is not valid YAML or has a misspelled setting, or a
       row of ``releases.csv`` is bad. Or a met directory does not exist
       or holds no ARL file (``runtraj check`` lists it; ``runtraj status`` exits
       with 2 for it only when the output may hold results). Nothing ran. Do
       not retry.
   * - 3
     - Retry it. Some simulations did not finish, because the run was
       stopped (Ctrl-C, or a stop signal from the scheduler), a worker
       process was killed (``--cpus`` above 1; the error names the releases
       that were in progress, and it is usually the out-of-memory killer, so a
       retry may need fewer ``--cpus``), or a connection or I/O error, such as
       an output bucket that could not be reached, stopped an unfinished
       simulation or ended the run. A retry continues where it stopped.
   * - 4
     - A bug: any other error the command did not expect. Its traceback is
       printed. Do not retry; a retry meets the same error.

An unexpected error ends with 3, not 4, when a retry may get past it:

- a refused, reset or timed-out connection, or a name that did not resolve,
  including the cloud libraries' own errors for these (botocore's
  ``EndpointConnectionError`` and ``ReadTimeoutError``)
- a busy or briefly unavailable service: HTTP 408, 429, 500, 502, 503 or
  504, an FTP 4xx reply such as "421 too many users", or an FTP server that
  closed the connection
- input/output trouble that goes away: ``EIO``, a stale NFS handle, a busy
  or interrupted call

Anything else is 4: another HTTP status (a bucket in another region, a
403, a 404), an FTP 5xx reply, a file that is not there or may not be used,
or a full disk. runtraj decides from the error and what caused it, not from
its type, because s3fs and fsspec's HTTP filesystem report many permanent
answers as a plain ``OSError`` or ``FileNotFoundError``. Every ``runtraj``
command follows this table, except that a closed standard output
(``runtraj status | head``) ends quietly with 1, and ``runtraj fetch`` gives 1
for a file the archive does not have.

The same rule applies to an error inside one simulation, such as a worker
that cannot write its result to the output bucket. An error a retry may get
past is not a failure of the simulation: no failure record is written (an
earlier attempt's is removed), the simulation stays ``pending``, or
``interrupted`` once its run had started, the other simulations go on, and
the run ends with 3. Any other error, a ``SimulationError`` such as a failed
HYSPLIT run included, is a failure record and exit 1. When both happen in
one run it exits 3, and the retry, which runs only what did not finish,
ends with 1 if the failure remains. A bucket that stays down keeps every
try at 3, so the program that drives the retries sets how many to make.

The built-in backends do not retry the same things. A Slurm task is put
back in the queue only after the notice before its time limit or a
preemption, at most ten times; a Kubernetes pod is started again on every
exit 3; PBS puts back only a preempted subjob. A scheduler you drive
yourself retries what you tell it to: ``-Q "3"`` in the LSF example above
puts back every task that exits with 3, a connection error included.

In Containers (Kubernetes)
==========================

A project can run in many containers at once, such as the pods of a
Kubernetes Job, without Slurm. ``runtraj submit`` makes that Job for you
(:ref:`kubernetes-submit`); the rest of this page is what each pod does,
for a Job you write yourself or another scheduler. :doc:`kubernetes_setup`
makes the image, the cluster and the bucket.

Each container runs one share of the releases, ``runtraj run /project
--task 3/20``, as a task on any scheduler does (:doc:`any_scheduler`:
how the releases are shared, what a task relies on, and its exit codes).

What every container needs
--------------------------

- runtraj installed (``pip install runtraj``), and HYSPLIT's ``hycs_std``
  on ``PATH`` or in the HYSPLIT folder ``directory`` names
  (:doc:`../getting_started/installation`). Every container needs the same
  ``hycs_std``: its checksum is part of each run's settings, so another
  build writes to another settings folder. A container that only reports
  status needs none.
- The project folder (``config.yaml`` and ``releases.csv``), the
  meteorology, and the output directory, on a volume all the containers
  share. Results are written there, one file per simulation, so the
  containers do not need to talk to each other. The output directory can
  instead be on an object store (``output: s3://bucket/...``), with the
  containers given its credentials.
  ``runtraj run --output s3://bucket/...`` uses another output directory
  than ``config.yaml`` names, without editing it.
- A compute root, where each run gets its workdir. Set
  ``RUNTRAJ_COMPUTE_ROOT`` to a folder on fast local disk, such as an
  ``emptyDir``.

.. _kubernetes-submit:

Submitting to Kubernetes
------------------------

With ``backend: kubernetes``, ``runtraj submit`` makes one indexed Job and
creates it with ``kubectl``, on the cluster and namespace ``kubectl``
points at. Each pod runs ``runtraj run <project> --task I/N``:

.. code-block:: yaml

   execution:
     backend: kubernetes
     kubernetes:
       image: registry.example.org/runtraj-hysplit:1
       completions: 20           # tasks, N
       parallelism: 10           # pods running at once
       cpus: 4                   # releases each pod runs at once
       memory: 8Gi               # Kubernetes units: 8Gi, not 8G
       inputs: s3://bucket/projects/my_project
       secrets: [bucket-keys]    # a Secret holding the bucket's credentials
       volumes:
         - {claim_name: hrrr-arl, mount_path: /met, read_only: true}
   output: s3://bucket/output

.. code-block:: bash

   runtraj submit /project

It prints the Job's name; ``kubectl get job <name>`` follows it and
``kubectl delete job <name>`` stops it. The manifest is kept in
``_kubernetes/<date_time>_<id>/job.yaml`` in the project.

We recommend putting the inputs on a bucket (``inputs``): ``runtraj submit``
copies ``config.yaml``, ``releases.csv`` and ``tables/`` there, and each pod
copies them to its own disk before it runs, so the pods need no shared
volume for the project. The output directory must then be on a bucket too,
or each pod would write its results to its own disk; ``runtraj submit``
refuses that. Without ``inputs`` the pods open the project folder at the
path it has where you submit, so it must be on a volume they mount at that
path. Copying never deletes: a ``sites.csv`` or a table you removed from the
project stays on the bucket, where the pods still read it, so ``submit`` and
``schedule`` log a warning naming the files the bucket has that the project
does not. Delete them there (``aws s3 rm``, for one).

``image`` is required. runtraj publishes no image: an image needs runtraj,
HYSPLIT's ``hycs_std`` (downloaded from NOAA ARL; :doc:`../getting_started/installation`)
and the packages of the stages the project names. Every pod must have the
same ``hycs_std``, since its checksum is part of each run's settings.

The block is checked when the project opens, so a setting Kubernetes would
refuse stops ``runtraj submit`` before anything reaches the cluster:
``image`` is not empty and has no space; ``namespace`` (one label),
``service_account``, each of ``secrets`` and each volume's ``claim_name``
are Kubernetes names (lowercase letters, digits and ``-``, starting and
ending with a letter or digit); and each volume's ``mount_path`` has no
``:`` (set ``read_only`` rather than writing ``/met:ro``), is not ``/`` or
``/scratch``, and is not another volume's.

Each pod gets an ``emptyDir`` at ``/scratch`` as its ``TMPDIR``, where its
copy of the inputs and its workdirs go, and one thread per worker. The Job
retries a pod from its exit code (:ref:`exit-codes`): 3 is retried up to
three times per task, 1 and 4 fail that task only, 2 fails the whole Job,
and a pod evicted or preempted is retried without counting. Each pod is
given the hash of ``config.yaml`` and ``releases.csv`` as they were
submitted (``--inputs-hash``) and exits with 2 when what it reads differs:
a ``releases.csv`` changed after submit would split the releases
differently among the tasks. Submit again after changing them.

A Job runs every unfinished release of the project and skips the complete
ones, so ``runtraj submit`` on Kubernetes takes no ``--releases`` and no
``--no-skip``. With a met that downloads, set ``fetch: true`` in the
block when the pods share the met volume, so the files are downloaded once
(:ref:`fetch-once`).
Afterwards ``runtraj status`` counts the project's simulations, as for any
run, and lists the project's Jobs on the cluster ``kubectl`` points at:

.. code-block:: text

   Project: /project  total=480  complete=455  failed=5  interrupted=0  pending=20
   Jobs:
     runtraj-my-project-x7k2p  running  17 of 20 tasks done, 2 running, 1 failed (tasks 6)  started 2026-10-29T16:00:00Z

A task counts as failed once Kubernetes gave up on it (after its retries,
or at once for exit 1 or 4); ``kubectl logs`` of its pod says why, and the
failed simulations each have a ``.failure.yaml``. The Jobs are found by the
project's name and kept when they were made for this project's inputs, so
another project of the same name is left out. When ``kubectl`` cannot list
them, the line says why and the counts still print. ``runtraj status
--json`` adds them as ``jobs``. When the output directory does not exist
(a typo in ``--output``, or nothing has run yet), ``status`` says so under
``Output:`` instead of only counting every simulation as pending, and
``--json`` has ``output_exists``.

.. _kubernetes-schedule:

Running every hour
------------------

A project for fixed sites, such as towers, can run on its own every hour:
each hour gets a release at each site once its met is out. List the sites in
``sites.csv`` in the project folder, one row each:

.. code-block:: text

   name,longitude,latitude,altitude,altitude_ref
   tower,-111.8476,40.7663,35,agl
   valley,-112.1155,40.5385,5,

``longitude``, ``latitude`` and ``altitude`` are required; ``altitude_ref``
(``agl``, the default, or ``msl``) is optional, and other columns, such as a
name, are ignored. A site that repeats an earlier line is used once, with a
warning that names both lines; a header without the required columns is
refused with the columns the file has. The project needs no
``releases.csv`` to start. Then,
with the same ``kubernetes`` block as for ``runtraj submit``:

.. code-block:: bash

   runtraj schedule /project

It makes a Kubernetes CronJob, with ``kubectl apply``, and prints its name.
At 15 minutes past each hour (UTC) the CronJob starts one pod, which runs
``runtraj tick``. A tick

1. adds a release at each site for each hour of the last day that is at
   least ``--delay`` hours old (8 by default, at most a week, 168), and
   reaches back up to a week
   further to close a gap after the newest hour ``releases.csv`` holds, as
   after an outage;
2. writes ``releases.csv`` back, to the bucket when the inputs are on one;
3. runs those releases in the pod, as ``runtraj run --releases`` would, and
   exits with ``runtraj run``'s code.

Releases whose results exist are skipped, so each tick runs the new hour
and any of the last day that failed before, such as an hour whose met was
not out yet. We recommend the default delay for HRRR: NOAA's archive posts
each 6-hour file about 40 minutes after its last hour, and a run also needs
the hour after its release, so an hour's met is all there about 7 hours 40
minutes later.

With ``inputs`` set, ``runtraj schedule`` copies ``config.yaml``,
``sites.csv`` and ``tables/`` there, and ``releases.csv`` only while the
bucket has none: from then on the bucket's copy is the one each hour adds
to, so read it there. Without ``inputs`` the pods open the project folder
at its path here, as for ``runtraj submit``. A tick locks ``releases.csv``
(a file ``.releases.csv.lock`` beside it) while it adds to it, so a tick
run by hand to fill in a past day waits for the hourly one rather than
writing over its hours:

.. code-block:: bash

   runtraj tick s3://bucket/projects/my_project --now 2026-10-01T00:00

``--now`` is the time to act as if it were, in UTC; a time later than the
clock is refused. Each hour's pod has 45 minutes, unless
``active_deadline_seconds`` gives it another limit, and a pod still running
holds back the next. Scheduling the project again, after editing
``sites.csv`` or the ``kubernetes`` block, replaces its CronJob, which is
named for the project and where its inputs are. The manifest is kept in
``_kubernetes/<date_time>_<id>/cronjob.yaml`` in the project;
``kubectl delete cronjob <name>`` stops the schedule. ``runtraj status``
lists each hour's Job with the others.

.. _fetch-once:

Fetching the meteorology once
-----------------------------

With ``download`` set, a run downloads the met files its hours need into the
met ``directory``. 50 pods whose releases fall on the same days, started
together, each download and crop the same files, even when they share the
met directory on one volume: none finds a file another is still getting.

On Kubernetes, put the met ``directory`` on a volume every pod mounts
(read-write, such as a ``ReadWriteMany`` claim) and set ``fetch``:

.. code-block:: yaml

   execution:
     backend: kubernetes
     kubernetes:
       image: registry.example.org/runtraj-hysplit:1
       fetch: true
       volumes:
         - {claim_name: hrrr-arl, mount_path: /met}

``runtraj submit`` then gives each pod ``--fetch``: before its share runs,
a pod gets the met files of every unfinished simulation, as ``runtraj
fetch`` does, holding a lock where the inputs are (``.fetch.lock`` beside
``config.yaml``, on the bucket or the project's volume). The pods take
their turn. The first downloads the files while the others wait; each
after it finds them there and downloads nothing, and starts its share. The
lock is rewritten every minute while it is held, so a long download keeps
it. Pods with their own met directories gain nothing from waiting, so
leave ``fetch`` off for them. With no met that downloads, the pods get no
``--fetch``.

``runtraj submit`` does the same for you on Slurm, with one fetch job
before the array (:ref:`slurm-fetch`). Anywhere else, ``runtraj fetch``
gets the files once, before the tasks start:

.. code-block:: bash

   runtraj fetch /project

For each met it gets the files that cover the time of every simulation that
has yet to run, cropped to the met's subgrid. They are the files ``runtraj
run`` would ask for, so a run that follows finds them all and downloads
nothing. Run it on one pod, with the met ``directory`` on the volume the
Job's pods then mount. Simulations that are finished need no met and are
left out; ``--output`` is as for ``runtraj run``, so it reads the same
results.

A met without ``download`` is only checked: ``runtraj fetch`` reports the
files it lacks, as ``runtraj check`` does, and does not make them.

It exits with:

- 0 when every file is there
- 1 when the archive does not have some files; it prints the times of the
  runs that need them
- 2 when the command line is wrong or a met directory cannot be read
- 3 when it was stopped (Ctrl-C, or the SIGTERM that Kubernetes and Slurm
  send), or the archive was busy or could not be reached, which a retry may
  get past
- 4 for any other unexpected error (:ref:`exit-codes`)

Run it again to get the missing files or to finish after a stop: the files
it got are kept. A stop removes the file being downloaded. A process killed
with SIGKILL, which cannot be caught, leaves a hidden
``.<name>.<random>.partial`` file in the met directory; delete it.

Fetches can overlap. Several ``runtraj fetch`` processes, or a fetch beside
runs, can share one met directory: each file is written under a temporary
name and renamed into place when it is whole, so no one reads half a file.
Two that start the same file both download it, and the second to finish
replaces the first with the same contents.

In Python, ``project.fetch()`` does the same and returns the files, one row
each, with the times it could not get files for.

A Kubernetes indexed Job
------------------------

An indexed Job gives each pod its index in ``JOB_COMPLETION_INDEX``, which
is the task number:

.. code-block:: yaml

   apiVersion: batch/v1
   kind: Job
   metadata:
     name: runtraj-my-project
   spec:
     completionMode: Indexed
     completions: 20          # N, the number of tasks
     parallelism: 20          # pods running at once
     backoffLimit: 6
     template:
       spec:
         restartPolicy: Never
         containers:
           - name: runtraj
             image: my-registry/runtraj:latest   # any image with runtraj
             command: ["sh", "-c"]
             args: ["exec runtraj run /project --task $JOB_COMPLETION_INDEX/20 --cpus 4"]
             env:
               - name: RUNTRAJ_COMPUTE_ROOT
                 value: /scratch
             resources:
               requests:
                 cpu: "4"
                 memory: 8Gi
             volumeMounts:
               - {name: project, mountPath: /project}
               - {name: met, mountPath: /met, readOnly: true}
               - {name: scratch, mountPath: /scratch}
         volumes:
           - name: project
             persistentVolumeClaim: {claimName: runtraj-project}
           - name: met
             persistentVolumeClaim: {claimName: hrrr-arl}
           - name: scratch
             emptyDir: {}

The met ``directory`` in ``config.yaml`` must be the path inside the
container (``/met`` here). ``completions`` and the ``N`` in ``--task`` must
match. ``--cpus 4`` runs four releases at once in each pod. ``exec`` makes
``runtraj`` the process that receives Kubernetes' stop signal, so a stopped
pod stops its runs cleanly and exits with code 3, within a few seconds of
the signal and so inside Kubernetes' default 30 s grace period. That holds
for ``--cpus`` above 1 too: the pod's workers and HYSPLIT runs stop with it.

Exit codes
----------

Each pod's exit code says whether to retry it (:ref:`exit-codes`). For a
Kubernetes indexed Job, a ``podFailurePolicy`` that retries on 3 and fails
the task on 1 and 4 follows from that table.

When the Job is done, ``runtraj status /project`` counts the finished and
failed simulations of the whole project. ``runtraj status /project --json``
gives the same counts, per state and variant, as JSON for the program
that drives the Job.

Setting Up Kubernetes
=====================

A project runs on Kubernetes once you have four things: a container image
with runtraj and HYSPLIT, a cluster that ``kubectl`` points at, a bucket for
the inputs and results that the pods can reach, and the met. runtraj keeps no
database and runs no service, so the Jobs and CronJobs of
:doc:`containers` are all it leaves on the cluster.

This page makes each of them. `Try it on your computer`_ runs a small
project on a cluster on your own machine first, with no cloud account.
`Google Cloud`_ makes a real cluster and bucket, and `Another cluster`_ says
what changes elsewhere.

.. _kubernetes-image:

The image
---------

runtraj publishes no image, since an image has to hold HYSPLIT's
``hycs_std``, which you download from NOAA ARL
(:doc:`../getting_started/installation`). Build one from a Linux HYSPLIT
folder (``exec/hycs_std`` and ``bdyfiles/``), copied next to this
``Dockerfile`` as ``hysplit/``:

.. code-block:: docker

   FROM python:3.13-slim
   RUN apt-get update \
       && apt-get install -y --no-install-recommends tini \
       && rm -rf /var/lib/apt/lists/*
   RUN pip install --no-cache-dir "runtraj[download]" gcsfs
   COPY hysplit/ /opt/hysplit/
   ENV PATH=/opt/hysplit/exec:$PATH
   RUN useradd --create-home --uid 10001 runtraj
   USER 10001
   ENTRYPOINT ["/usr/bin/tini", "--"]

``gcsfs`` reads and writes a Google Cloud Storage bucket; use ``s3fs`` for
an S3 one. ``tini`` runs as process 1, passes Kubernetes' stop signal on to
``runtraj`` (which exits with 3 and is retried) and reaps the finished
HYSPLIT processes. The image runs as user 10001 rather than root, the group
runtraj gives the pods' volumes (``fsGroup``), so a new volume is writable
without further setup. Add the package of each stage your project names, and pin the
versions you ran with. Then build it, and push it to a registry the cluster
can pull from:

.. code-block:: bash

   docker build -t us-central1-docker.pkg.dev/my-project/runs/runtraj-hysplit:1 .
   docker push us-central1-docker.pkg.dev/my-project/runs/runtraj-hysplit:1

and name it in the project's ``config.yaml``:

.. code-block:: yaml

   execution:
     backend: kubernetes
     kubernetes:
       image: us-central1-docker.pkg.dev/my-project/runs/runtraj-hysplit:1

We recommend a private registry: the image holds HYSPLIT, whose terms are
NOAA ARL's. Every pod must run the same ``hycs_std``, since its checksum is
part of each run's settings (:ref:`kubernetes-submit`), and ``runtraj
submit`` and ``status`` look for the results by the checksum of the
``hycs_std`` where they run: submit and check from a machine with the same
build. Give each build a new tag rather than reusing one, so that the pods
of a Job all pull the same image.

Try it on your computer
-----------------------

This runs a small project on a Kubernetes cluster on your own computer,
with `kind <https://kind.sigs.k8s.io/>`_ (Kubernetes in Docker) and a
stand-in S3 server inside the cluster. It is the run runtraj's end-to-end
test makes on every pull request, so it is known to work. It takes about
ten minutes, on Linux: the HYSPLIT build below is for Linux on x86-64.

You need Docker, ``kind``, ``kubectl``, `uv <https://docs.astral.sh/uv/>`_
and a clone of the repository:

.. code-block:: bash

   git clone https://github.com/jmineau/runtraj.git
   cd runtraj
   uv sync --extra download
   uv pip install s3fs

The project is ``tests/kubernetes/demo``: two releases at the University of
Utah early on 2021-01-15, three hours back, 100 particles each, with its
inputs and results on the stand-in S3 server (its ``config.yaml`` has the
``kubernetes`` block). It needs a Linux HYSPLIT folder at ``/opt/hysplit``,
here and in the image. The script the test uses installs one (it asks for
``sudo``):

.. code-block:: bash

   .github/scripts/install_hysplit.sh

The meteorology
^^^^^^^^^^^^^^^

The pods read the met from a folder your computer shares with the cluster.
Download the one HRRR file the project needs, cropped to Utah (about 30 MB
once cropped; the download itself is 3.4 GB):

.. code-block:: bash

   uv run python -c '
   from arlmet.archives import HRRRArchive
   HRRRArchive().fetch("2021-01-15 00:00", "2021-01-15 05:59", dest_dir="met",
                       bbox=(-114.0, 39.0, -110.0, 42.0))
   '

runtraj reads the met's first file to name the results, so the met must be
at ``/met`` on your computer too, as in the pods:

.. code-block:: bash

   sudo ln -s "$PWD/met" /met

The cluster
^^^^^^^^^^^

Make a cluster whose node sees that folder at ``/met``:

.. code-block:: bash

   cat > kind.yaml <<EOF
   kind: Cluster
   apiVersion: kind.x-k8s.io/v1alpha4
   nodes:
     - role: control-plane
       extraMounts:
         - hostPath: $PWD/met
           containerPath: /met
           readOnly: true
   EOF
   kind create cluster --name runtraj --config kind.yaml

Build the image the demo names (``runtraj:ci``), from this checkout and the
HYSPLIT folder, and give it to the cluster:

.. code-block:: bash

   uv build --wheel --out-dir image/dist
   cp -r /opt/hysplit image/hysplit
   docker build -t runtraj:ci -f tests/kubernetes/Dockerfile image
   kind load docker-image runtraj:ci --name runtraj

Start the S3 server. ``tests/kubernetes/cluster.yaml`` also makes the
Secret that tells the pods where it is (``AWS_ENDPOINT_URL``) and a
PersistentVolumeClaim, ``met``, for the met folder:

.. code-block:: bash

   kubectl apply -f tests/kubernetes/cluster.yaml
   kubectl wait --for=condition=available deployment/s3 --timeout=300s
   kubectl port-forward service/s3 5000:5000 &

Run the project
^^^^^^^^^^^^^^^

Point your own shell at the same S3 server, through the port forward, and
make the bucket:

.. code-block:: bash

   export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
   export AWS_DEFAULT_REGION=us-east-1 AWS_ENDPOINT_URL=http://127.0.0.1:5000
   uv run python -c "import s3fs; s3fs.S3FileSystem().mkdir('runtraj')"

Then submit and check, as on a real cluster:

.. code-block:: bash

   uv run runtraj submit tests/kubernetes/demo
   kubectl get pods --watch                     # Ctrl-C when both are Completed
   uv run runtraj status tests/kubernetes/demo

.. code-block:: text

   Project: .../tests/kubernetes/demo  total=2  complete=2  failed=0  interrupted=0  pending=0
   Jobs:
     runtraj-demo-2j4rc  complete  2 of 2 tasks done, 0 running  started 2026-11-21T00:24:32Z

``kubectl logs`` on a pod shows its ``runtraj run``. Submit again and
nothing is submitted: both simulations are complete. The results are
ordinary runtraj output on the bucket, under ``s3://runtraj/output``.

When you are done:

.. code-block:: bash

   kind delete cluster --name runtraj
   sudo rm /met

.. note::

   ``AWS_ENDPOINT_URL`` sends every S3 request to that server, including a
   met's ``download`` requests to NOAA's bucket. With an S3-compatible
   store such as MinIO, give the pods their met on a volume.

Google Cloud
------------

You need a Google Cloud project with billing, the
`gcloud CLI <https://cloud.google.com/sdk/docs/install>`_, and ``kubectl``
with its Google sign-in plugin:

.. code-block:: bash

   gcloud components install kubectl gke-gcloud-auth-plugin

   export PROJECT=my-project ZONE=us-central1-a
   gcloud config set project $PROJECT
   gcloud services enable container.googleapis.com storage.googleapis.com \
       artifactregistry.googleapis.com

A registry for the image (`The image`_), private to the project:

.. code-block:: bash

   gcloud artifacts repositories create runs --repository-format=docker \
       --location=us-central1
   gcloud auth configure-docker us-central1-docker.pkg.dev

The bucket
^^^^^^^^^^

.. code-block:: bash

   gcloud storage buckets create gs://my-runs --location=us-central1 \
       --uniform-bucket-level-access

The inputs go under ``gs://my-runs/projects/``, and the results under
``gs://my-runs/output``, so ``config.yaml`` says:

.. code-block:: yaml

   output: gs://my-runs/output
   execution:
     backend: kubernetes
     kubernetes:
       image: us-central1-docker.pkg.dev/my-project/runs/runtraj-hysplit:1
       inputs: gs://my-runs/projects/my_project

The cluster
^^^^^^^^^^^

Make a cluster with one small node, which runs Kubernetes' own pods, and a
pool of bigger nodes for the runs. The pool has no nodes until a Job asks
for them, and goes back to none when the Job is done, so it costs nothing
between runs:

.. code-block:: bash

   gcloud container clusters create runs --zone=$ZONE \
       --workload-pool=$PROJECT.svc.id.goog \
       --addons=GcpFilestoreCsiDriver \
       --machine-type=e2-standard-2 --num-nodes=1
   gcloud container node-pools create work --cluster=runs --zone=$ZONE \
       --machine-type=e2-standard-8 --spot \
       --enable-autoscaling --min-nodes=0 --max-nodes=20
   gcloud container clusters get-credentials runs --zone=$ZONE

``--spot`` makes the pool's nodes Spot VMs, much cheaper, which Google can
take back at any time. A pod lost that way is retried without counting
against its task's retries, so nothing is lost but time; leave ``--spot``
out for nodes that stay. ``--max-nodes`` caps the cost of one run.
``--addons=GcpFilestoreCsiDriver`` is for the met volume below.

A pod asks for ``cpus`` CPUs, and a node keeps a little of its own, so a pod
that asks for all 8 of an 8-CPU machine never starts. Ask for a bit less
(``cpus: 7``), or for half (``cpus: 4``, two pods to a node).

The pods' access to the bucket
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``--workload-pool`` lets a Kubernetes service account act on the bucket
with no key to keep. Make one and give it the bucket:

.. code-block:: bash

   kubectl create serviceaccount runs
   NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
   MEMBER=principal://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/$PROJECT.svc.id.goog/subject/ns/default/sa/runs
   gcloud storage buckets add-iam-policy-binding gs://my-runs \
       --role=roles/storage.objectUser --member=$MEMBER

``roles/storage.objectUser`` reads, writes and deletes files in that bucket
only. Name the service account in the ``kubernetes`` block:

.. code-block:: yaml

   execution:
     backend: kubernetes
     kubernetes:
       image: us-central1-docker.pkg.dev/my-project/runs/runtraj-hysplit:1
       service_account: runs

Your own computer reads and writes the bucket too (``runtraj submit``
copies the inputs there, ``runtraj status`` counts the results). Sign in
once, and install ``gcsfs``:

.. code-block:: bash

   gcloud auth application-default login
   pip install runtraj gcsfs

The met
^^^^^^^

With ``download: hrrr``, the met comes from NOAA's archive, which is
public. Keep it on a volume every pod mounts, so each file is downloaded
once, and set ``fetch`` so the pods get it in turn before they run
(:ref:`fetch-once`). The pods run on many nodes at once, so the volume must
be one that many nodes can mount and write (``ReadWriteMany``). On Google
Cloud that is Filestore, whose driver the cluster above has:

.. code-block:: bash

   kubectl apply -f - <<EOF
   apiVersion: v1
   kind: PersistentVolumeClaim
   metadata:
     name: met
   spec:
     accessModes: [ReadWriteMany]
     storageClassName: standard-rwx
     resources:
       requests:
         storage: 1Ti
   EOF

Then point the met at it, cropped to your region, and mount it:

.. code-block:: yaml

   mets:
     hrrr:
       download: hrrr
       directory: /met/hrrr
       subgrid_enable: true
       subgrid_bounds: {xmin: -114.0, xmax: -110.0, ymin: 39.0, ymax: 42.0}
   execution:
     backend: kubernetes
     kubernetes:
       image: us-central1-docker.pkg.dev/my-project/runs/runtraj-hysplit:1
       fetch: true
       volumes:
         - {claim_name: met, mount_path: /met}

Each 6-hour HRRR file is 3.4 GB before it is cropped; cropped to the box
above, a year of HRRR is about 30 GB. Filestore is billed for the size you
ask for, used or not.

A met directory on the pod's own disk, such as ``/scratch/met``, works
without a volume, but each pod then downloads every file its releases
need, and the releases of each day are spread over all the pods, so nearly
every pod downloads nearly every file. Leave ``fetch`` off then.

``runtraj schedule`` runs one pod at a time, so any volume works for a
schedule, also a ``ReadWriteOnce`` disk (``accessModes: [ReadWriteOnce]``
and no ``storageClassName``), which costs less.

Run it
^^^^^^

.. code-block:: bash

   runtraj submit ./my_project
   runtraj status ./my_project

:doc:`containers` has the rest: the retries, what ``status`` lists, the
hourly schedule, and the exit codes.

Another cluster
---------------

Any Kubernetes cluster works, with an ``s3://`` bucket on AWS or any
S3-compatible store. Without a workload identity, put the bucket's keys in
a Secret, and name it in ``secrets``; its keys become the pods'
environment:

.. code-block:: bash

   cat > aws.env <<EOF
   AWS_ACCESS_KEY_ID=...
   AWS_SECRET_ACCESS_KEY=...
   AWS_DEFAULT_REGION=us-west-2
   EOF
   kubectl create secret generic bucket-creds --from-env-file=aws.env
   rm aws.env

.. code-block:: yaml

   output: s3://my-bucket/output
   execution:
     backend: kubernetes
     kubernetes:
       image: registry.example.org/runtraj-hysplit:1
       inputs: s3://my-bucket/projects/my_project
       secrets: [bucket-creds]

On AWS's own clusters (EKS), an IAM role for a service account does what
Workload Identity does above: name that service account in
``service_account`` instead, and give the met an EFS volume
(``ReadWriteMany``). A store other than AWS also needs ``AWS_ENDPOINT_URL``
in the Secret; then the met must come from a volume, since a ``download``
would go to that store too (`Try it on your computer`_).

When you are done
-----------------

Delete the met volume first, so its Filestore goes with it, then the
cluster:

.. code-block:: bash

   kubectl delete pvc met
   gcloud container clusters delete runs --zone=$ZONE

The bucket stays, with the results. ``gcloud storage rm --recursive
gs://my-runs`` deletes it and everything in it.

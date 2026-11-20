The Output Directory
====================

Every result of a project goes to its :term:`output directory`, which
``output:`` in ``config.yaml`` names. You need to know its layout to find a
log, to read results with tools other than runtraj, or to put the results
on an object store. Any path works, and projects that name the same
directory share it and the results in it, so we keep one output directory
for all the projects of a study.

What is in it
-------------

.. code-block:: text

   output/
     particles/
       settings=hrrr-2e01ed/                 # one folder per set of transport settings
         _settings.yaml                      # the settings, written out in full
         date=2021-01-15/<release id>.parquet
     logs/
       settings=hrrr-2e01ed/date=2021-01-15/<release id>.log
       settings=hrrr-2e01ed/date=2021-01-15/<release id>.failure.yaml   # a failed run
     scratch/
       settings=hrrr-2e01ed/date=2021-01-15/<release id>/              # a kept workdir

Each result file is named by its release's id (:doc:`releases`), in a
folder for the day of the release time, UTC. A project with a stage also
has a tree named for it, beside ``particles/``.

Each :term:`settings folder` is named after the variant that first made it,
plus six characters computed from the settings it was made with. So a
changed setting never overwrites a result: edit ``ziscale`` and the next
run writes into a new folder beside the old one, and projects with the same
settings share a folder (:doc:`projects`, "Changing a setting"). The
``_settings.yaml`` in each folder lists the settings in full, so a folder
explains itself. An ensemble (``realizations: N``) has a ``realization=k``
folder between the settings folder and the dates.

The logs and the kept workdirs are explained in :doc:`checking`. The folder
names are what pyarrow, DuckDB, polars, and R's ``arrow`` read as columns,
so ``output/particles`` opens as one table with ``settings`` and ``date``
columns, no runtraj needed (:doc:`particles`).
:doc:`../reference/layout` describes every file, for a reader written
without runtraj.

On an object store
------------------

The output directory can be on an object store such as Amazon S3 or Google
Cloud Storage. Write it as a URL:

.. code-block:: yaml

   output: s3://my-bucket/slv-2021

runtraj reads and writes it through `fsspec
<https://filesystem-spec.readthedocs.io>`_, so install the package for your
store: ``pip install s3fs`` for ``s3://``, ``gcsfs`` for ``gs://``.
Credentials come from where those packages look for them, such as
``~/.aws/credentials`` or ``AWS_ACCESS_KEY_ID``. For another store that
speaks the S3 protocol, set ``AWS_ENDPOINT_URL``.

Everything else works the same: the folder layout, reruns that skip
finished simulations, ``runtraj status``, and reading results. Each result
is written straight to the store, while HYSPLIT itself still runs in a
:term:`workdir` on local disk (``--compute-root`` or
``RUNTRAJ_COMPUTE_ROOT``). Listing an object store takes longer than
listing a disk, so ``runtraj status`` on a large project is slower.

Another output directory for one command
----------------------------------------

To use another output directory than ``config.yaml`` names, without editing
it, give ``--output`` to ``runtraj run``, ``submit``, ``fetch``, or
``status``, or open the project with
``runtraj.Project(path, output="s3://my-bucket/slv-2021")``.

When a result is missing or a run failed, :doc:`checking` says where to
look.

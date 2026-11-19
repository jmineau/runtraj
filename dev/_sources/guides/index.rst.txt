User Guide
==========

These pages show how to set up a project and run it. If you are new to
runtraj, start with :doc:`../getting_started/quickstart`.

**Setting up a project**

- :doc:`releases`: releases, the times and places particles start from
  (a point, a vertical column, or many points at once)
- :doc:`meteorology`: use your own ARL files or download them from NOAA

**Running**

- :doc:`running`: run on your computer, and the commands
- :doc:`slurm`: run thousands of simulations on an HPC cluster
- :doc:`containers`: run with Kubernetes or another scheduler

**HYSPLIT**

- :doc:`hysplit_users`: HYSPLIT's starting points, settings and particle
  output in runtraj, a single trajectory, and many runs as a project
- :doc:`hysplit_build`: run another HYSPLIT build, or your own land-use
  and terrain tables

The pages on a project's settings and variants, checking a run, choosing
settings, and reading and plotting the particles have not been written
yet. Until they are, :doc:`../reference/configuration` lists every
``config.yaml`` option, and the :doc:`../reference/index` has every method.

.. toctree::
   :hidden:
   :caption: Setting up a project

   releases
   meteorology

.. toctree::
   :hidden:
   :caption: Running

   running
   slurm
   containers

.. toctree::
   :hidden:
   :caption: HYSPLIT

   hysplit_users
   hysplit_build

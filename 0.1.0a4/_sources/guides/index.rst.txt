User Guide
==========

Each page covers one task, in the order you meet them. If you are new to
runtraj, start with :doc:`../getting_started/quickstart`, and look up a
word in the :doc:`../getting_started/glossary`.

**Setting up a project**

- :doc:`projects`: the project folder, ``config.yaml``, and variants; how
  reruns and changed settings work
- :doc:`releases`: releases, the times and places particles start from
  (a point, a vertical column, or many points at once)
- :doc:`meteorology`: use your own ARL files or download them from NOAA
- :doc:`choosing_settings`: how long to run, how many particles, random
  numbers, and what HYSPLIT chooses for you

**Running**

- :doc:`running`: run on your computer, and the commands
- :doc:`checking`: find problems before a run, see where each simulation
  stands, and find out why one failed
- :doc:`slurm`: run thousands of simulations on an HPC cluster
- :doc:`containers`: run with Kubernetes or another scheduler
- :doc:`kubernetes_setup`: the image, a cluster and a bucket for Kubernetes,
  on your computer or on Google Cloud

**Results**

- :doc:`output`: what is in the output directory, and putting it on an
  object store
- :doc:`particles`: read and plot the particles of one simulation or
  many, and open them in DuckDB, R, or CF trajectory tools
- :doc:`concentration`: HYSPLIT's concentration grids, of pollutants you
  release, in place of particles
- :doc:`trajectories`: HYSPLIT's single trajectories, one from each
  release location, with no turbulence

**HYSPLIT**

- :doc:`hysplit_users`: HYSPLIT's starting points, settings and particle
  output in runtraj, a single trajectory, and many runs as a project
- :doc:`hysplit_build`: run another HYSPLIT build, or your own land-use
  and terrain tables

.. toctree::
   :hidden:
   :caption: Setting up a project

   projects
   releases
   meteorology
   choosing_settings

.. toctree::
   :hidden:
   :caption: Running

   running
   checking
   slurm
   containers
   kubernetes_setup

.. toctree::
   :hidden:
   :caption: Results

   output
   particles
   concentration
   trajectories

.. toctree::
   :hidden:
   :caption: HYSPLIT

   hysplit_users
   hysplit_build

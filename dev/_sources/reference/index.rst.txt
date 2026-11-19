API Reference
=============

Every public class and function, grouped by what it is for. To find out how
to do something, start with the :doc:`../guides/index` instead.

:doc:`core`
   ``Project``, releases, simulations, particles, and grids.

:doc:`particles`
   The particle table a transport model run gives: its columns and units.

:doc:`transforms`
   Weighting particle rows (averaging kernels, pressure weighting, loss
   along the way), and sampling a gridded field at particles.

:doc:`layout`
   The files of an output directory, for reading them without runtraj.

:doc:`configuration`
   Every ``config.yaml`` option.

:doc:`meteorology`
   Finding, downloading, and cropping meteorology files.

:doc:`execution`
   Running simulations locally or on Slurm, transport models, and stages.

:doc:`project`
   The project, its simulations, and the output directory.

:doc:`hysplit`
   The low-level HYSPLIT driver.

:doc:`exceptions`
   The exceptions runtraj raises.

.. toctree::
   :maxdepth: 2

   core
   particles
   transforms
   layout
   configuration
   meteorology
   execution
   project
   hysplit
   exceptions

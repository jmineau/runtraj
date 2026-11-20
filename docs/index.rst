runtraj
=======

.. image:: https://zenodo.org/badge/DOI/10.5281/zenodo.24384235.svg
   :target: https://doi.org/10.5281/zenodo.24384235
   :alt: DOI

.. rst-class:: hero-copy

runtraj runs particle dispersion models at scale. Give it the times and
places particles are released from, some meteorology, and the settings to
run under, and it runs the transport model once for each, on your computer
or as a Slurm job array, and keeps the particles where you can find them
again.

`HYSPLIT <https://www.ready.noaa.gov/HYSPLIT.php>`_ is the transport model
built in. Another model, and what is made from the particles after a run
(a stage), plug in by the import path of a class. The footprints of
`PYSTILT <https://jmineau.github.io/PYSTILT/>`_ are made by such a stage.

The particles follow the air, backward from a measurement or forward from
a source, over hours to days, at the resolution of the meteorology you give
it. They carry no mass and do not settle. A transform weights each
particle's rows for a species lost on the way (to a lifetime, deposition,
or oxidation) or for a column measurement, and runtraj writes particle
positions, not concentration grids.

.. note::

   runtraj is alpha software. Names and options may still change between
   releases.

.. grid:: 1 1 2 2
   :gutter: 2

   .. grid-item-card:: :fas:`play` Getting Started
      :link: getting_started/index
      :link-type: doc

      Install runtraj, get HYSPLIT, and run your first release.

   .. grid-item-card:: :fas:`book` User Guide
      :link: guides/index
      :link-type: doc

      Set up a project, choose its settings, run it on a cluster, check
      the runs, and read the particles.

   .. grid-item-card:: :fas:`code` API Reference
      :link: reference/index
      :link-type: doc

      Every class, function, and configuration option.

   .. grid-item-card:: :fas:`wrench` Development
      :link: development/index
      :link-type: doc

      How the package is put together, and how to add a transport model or
      a stage.

Run HYSPLIT on its own? :doc:`guides/hysplit_users` maps ``CONTROL`` and
``SETUP.CFG`` to runtraj's settings.

If you use runtraj in published work, please cite it through its
`Zenodo DOI <https://doi.org/10.5281/zenodo.24384235>`_, which always points to the latest release.

.. toctree::
   :hidden:
   :maxdepth: 2

   getting_started/index
   guides/index
   reference/index
   development/index

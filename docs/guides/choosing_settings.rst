Choosing Settings
=================

Three settings decide most of what a study's particles say: which
meteorology, how long the particles run (``n_hours``), and how many there
are (``numpar``). The right value of each depends on your site and your
question. Here is how we choose each one, and a quick check that tells you
whether the choice was good enough.

.. This page runs when the docs are built, on the sample project of
   docs/_ext/docs_examples.py (four releases on 15 January 2021).

.. ipython:: python
   :suppress:

   import os

   import docs_examples

   docs_cwd = docs_examples.temp_workdir(sample=True)

Meteorology
-----------

Use the finest product that covers your place and years, because the
valleys, coastlines and mixed-layer depth that steer air near the ground
are smoothed away on a coarse grid. In the contiguous US since mid-2015 that
is HRRR at 3 km. :doc:`meteorology` has a table of the products and when to
use each. To see how much the choice matters for your site, run the same
releases with a second met as a second variant and compare the particles.

How long to run
---------------

``n_hours`` has no default, because it depends on the question. For a
backward run from a measurement, it should be long enough for the air to
leave the region whose sources you care about. We start with 24 hours for a
tower in a city and 72 hours or more for a regional network or a remote
site. For a forward run from a source, run until the particles have left the
area you study.

The check is the particles themselves: how long they ran, and where they
ended. ``particles.runtraj.endpoints()`` gives the last row of each
particle:

.. ipython:: python

   import runtraj

   project = runtraj.Project("./my_project")
   sim = project.simulation("202101150800_-111.848_40.766_10", "hrrr")
   ends = sim.particles.runtraj.endpoints()
   print(f"the longest particle ran {ends['age'].abs().max() / 60:.1f} of 24 hours")

These particles stopped before 24 hours because they left the small crop of
met files these pages run on. The met's domain, not ``n_hours``, limited this
run, so a study would widen the crop (:doc:`meteorology`). When the
particles do run the whole time and many of them still end inside your
region, a longer run would follow them further, so raise ``n_hours``.

How many particles
------------------

More particles sample the turbulence better, and the far end of a run, where
the particles have spread out, needs the most. 200 particles show where the
air went. For results we use 1000. To check a value for your site, run a few
releases with three times as many particles as a second variant and
compare:

.. code-block:: yaml

   variants:
     hrrr: {}
     hrrr-np3k: {numpar: 3000}

Compare what your study computes from the particles, not the particles
themselves. If the two counts give results that differ by much less than
the other errors in your study, the lower count is enough. The variant with
more particles has its own settings folder, so the check leaves your results
as they are.

Random numbers
--------------

HYSPLIT draws random numbers for the turbulence that moves each particle.
By default it seeds them from the clock, so every run gives other particles.
``krand: 2`` with a ``seed`` makes the draws the same on every run, so a
rerun or a colleague's run gives the same particles:

.. code-block:: yaml

   models:
     hysplit:
       n_hours: -24
       numpar: 1000
       krand: 2
       seed: 42

We set both for any study we may have to reproduce. An ensemble
(``realizations: N``, :doc:`projects`) needs them, so that each realization
gets its own draws (``seed + k``).

.. ipython:: python
   :suppress:

   os.chdir(docs_cwd)

What else to set
----------------

Most projects leave the other HYSPLIT settings at HYSPLIT's own defaults.
:doc:`hysplit_users` lists them under HYSPLIT's names, and
:doc:`../reference/configuration` describes each one. A setting changed in a
variant makes its own settings folder, so you can try one beside your
defaults without losing a result.

When a result looks wrong, :doc:`checking` lists what to check, in order.

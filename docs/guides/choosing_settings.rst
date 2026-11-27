Choosing Settings
=================

Four choices decide most of what a study's particles say: which
meteorology, how long the particles run (``n_hours``), how many there are
(``numpar``), and how their random numbers are drawn. The right value of
each depends on your site and your question. Here is how we choose each
one, a quick check that tells you whether the choice was good enough, and
what HYSPLIT chooses for you when you set nothing.

The examples on this page that run use a small sample project: four
releases an hour apart on the morning of 15 January 2021 at one site in Salt
Lake City, 24-hour backward runs with 100 particles, and one variant,
``hrrr``.

.. This page runs when the docs are built, on the sample project of
   docs/_ext/docs_examples.py (four releases on 15 January 2021).

.. ipython:: python
   :suppress:

   import os

   import docs_examples

   docs_cwd = docs_examples.temp_workdir(sample=True)

Meteorology
-----------

Use the finest product that covers your place and years.
:doc:`meteorology` ("Which weather product") says which that is and why. The
grid of the met files sets the smallest scale the particles resolve: a
valley narrower than a few grid cells is not in the winds. To see how much
the choice matters for your site, run the same releases with a second met as
a second variant and compare them (`Comparing two variants`_).

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

More particles sample the turbulence better. The far end of a run, where
the particles have spread out and few are in any one place, converges last.
100 particles show where the air went. For results we use 1000, below the
default of 2500, because it is enough for a tower and a project of
thousands of releases runs in less than half the time. To check a value for
your site, run a few releases with three times as many particles as a
second variant:

.. code-block:: yaml

   variants:
     hrrr: {}
     hrrr-np3k: {numpar: 3000}

Compare what your study computes from the particles, such as a value at the
endpoints, and look hardest at the parts that depend on the far end of the
run. If the two counts give results that differ by much less than the other
errors in your study, the lower count is enough. The variant with more
particles has its own settings folder, so the check leaves your results as
they are.

Random numbers
--------------

HYSPLIT draws random numbers for the turbulence that moves each particle,
and ``krand`` says how. Left unset, HYSPLIT picks a mode for you (2 for up
to 5000 particles). We set ``krand: 2`` and a ``seed`` in every study, so
the particles depend on a number written in ``config.yaml`` and a rerun or
a colleague's run gives the same particles:

.. code-block:: yaml

   models:
     hysplit:
       n_hours: -24
       numpar: 1000
       krand: 2
       seed: 42

With them, two variants start from the same random numbers, so the
difference between ``hrrr`` and ``hrrr-zi08`` comes from ``ziscale`` and
not from a different draw. An ensemble (``realizations: N``,
:doc:`projects`) runs realization ``k`` with ``seed + k``, so each gets its
own draws. A ``seed`` must be 0 or more, because HYSPLIT would run a seed and
its negative the same. ``krand: 4`` seeds each run from the clock instead, so every run
differs and cannot be made again. ``krand`` in
:doc:`../reference/configuration` lists every mode.

.. note::

   HYSPLIT 5.0.x never seeds the random numbers of a column release or of
   an ``ichem: 5`` run, so on it those runs differ every time whatever
   ``seed`` is. Use HYSPLIT 5.1.0 or later to repeat them
   (:ref:`hysplit-versions`).

What HYSPLIT chooses for you
----------------------------

Every setting you leave out takes a default, and some of them shape the
particles. These are the ones we would look at before trusting a
sensitivity result. Unset, the mixing and turbulence choices are HYSPLIT's
own for its particle output mode (``ICHEM=8``), which runtraj always uses.

.. list-table::
   :header-rows: 1
   :widths: 18 22 60

   * - Setting
     - Default
     - What it means
   * - ``kmixd``
     - unset: 3
     - The mixed-layer depth, the layer next to the ground that turbulence
       mixes, is computed by HYSPLIT from a Richardson number. ``0`` takes
       it from the met files instead. ``ziscale`` multiplies whichever is
       used.
   * - ``kmix0``
     - ``150``
     - The mixed layer is never shallower than 150 m, after ``ziscale``.
   * - ``kblt``
     - unset: 5
     - The Hanna boundary-layer turbulence scheme.
   * - ``capemin``
     - unset: -2
     - The Grell convection scheme is on.
   * - ``emisshrs``
     - ``0.01``
     - The particles are released over 0.01 hours (36 seconds), close to
       an instant. For a source that emits over a period, set it in hours.
   * - ``outdt``
     - ``0``
     - A row is written every time step.
   * - ``z_top``
     - ``25000``
     - The top of the model domain, in meters above ground.

The particles carry no mass and follow the air without settling. A species
lost on the way is counted by weighting the particles' rows afterward
(:doc:`particles`, "Weighting the particles"). :doc:`hysplit_users` maps every setting to its
line in HYSPLIT's ``CONTROL`` and ``SETUP.CFG``, and
:doc:`../reference/configuration` describes each one.

Comparing two variants
----------------------

To see what one setting does, run it as a second variant and load both into
one table. ``project.particles`` adds a ``variant`` column, so pandas can
summarize each:

.. code-block:: python

   sims = project.simulations
   both = project.particles(sims[sims.variant.isin(["hrrr", "hrrr-zi08"])])

   # where each particle of each simulation ended
   last = both.groupby(["variant", "release", "particle"]).age.transform(
       lambda age: age.abs() == age.abs().max()
   )
   both[last].groupby("variant")[["zagl", "lon", "lat"]].describe()

With ``krand: 2`` and a ``seed`` set, the two variants start from the same
random numbers, so a difference between them comes from the setting.

.. ipython:: python
   :suppress:

   os.chdir(docs_cwd)

When a result looks wrong, :doc:`checking` lists what to check, in order.

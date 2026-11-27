Concentration Grids
===================

Use this page when you want what HYSPLIT is best known for: how much of a
pollutant is where, on a grid, over time. HYSPLIT counts its particles on
the grids you give it and writes a concentration file for each. runtraj
runs it in that mode with ``mode: concentration`` in the ``hysplit``
block, and gives you each release's grids as one
:class:`xarray.DataTree`, a :class:`xarray.Dataset` per grid. We recommend
it when you need concentrations or deposition and not the path of each
particle: the result is far smaller than the particle table.

.. ipython:: python
   :suppress:

   import docs_examples

   met_dir = docs_examples.met()

One run
-------

A concentration run needs at least one pollutant, what is released, and at
least one grid, where it is counted. Here a pollutant ``TEST`` is released
at 1 unit of mass per hour for one hour from Salt Lake City, and counted
for 3 hours on a grid of 0.02° cells around the release, in a layer 100 m
deep, as hourly averages:

.. ipython:: python

   import runtraj
   from runtraj.models import run_model
   from runtraj.models.hysplit import HysplitConfig, HysplitMetConfig

   start = runtraj.PointRelease(
       time="2021-07-15 01:00", longitude=-111.85, latitude=40.77, altitude=10
   )
   config = HysplitConfig(
       n_hours=3,
       numpar=1000,
       mode="concentration",
       pollutants=[{"name": "TEST", "rate": 1.0, "hours": 1.0}],
       grids=[{"name": "slc", "spacing_lat": 0.02, "spacing_lon": 0.02, "levels": [100]}],
       krand=2,
       seed=42,
   )
   tree = run_model("hysplit", start, config, HysplitMetConfig.model_validate(met_dir)).result
   slc = tree["slc"].to_dataset()
   slc

``time`` is when each sample starts and ``time_end`` when it ends; ``z`` is
the top of each level in meters above ground. Each pollutant is a variable
of its own name (never ``lat``, ``lon``, ``z`` or ``time``, which are
coordinates). Its unit is the unit of mass of its ``rate`` per cubic
meter, so with a rate in grams per hour, the concentration is in g/m³.
With ``cmass: 1`` each cell holds the mass in it instead, in grams, and
no deposition is counted at ``z = 0``; the variable's ``units`` and
``long_name`` attributes say which you have. The cells HYSPLIT did not write are zero. Where did the most pass in the
first hour?

.. ipython:: python

   first = slc["TEST"].isel(time=0, z=0)
   first.where(first == first.max(), drop=True)

HYSPLIT's own words for these settings, and the lines of ``CONTROL`` they
go to, are in :ref:`hysplit-concentration`.

Pollutants and grids
--------------------

``pollutants`` and ``grids`` are lists, one record each. Every pollutant is
released at every release location and counted on every grid. A pollutant
is HYSPLIT's four-letter identifier and its emission, and, for a pollutant
that is removed, its deposition, wet removal and decay in HYSPLIT's units
(:class:`~runtraj.models.hysplit.Pollutant`). All zero, the default, is a
gas that is not removed.

A grid is a regular latitude-longitude grid
(:class:`~runtraj.models.hysplit.ConcentrationGrid`): its center (unset,
the release; we recommend this for releases at many places), its spacing
and span in degrees, its levels, and its samples. ``sampling`` is
``average`` (the mean over each interval, the default), ``snapshot`` (the
value at the interval's end), or ``maximum``; ``sampling_interval`` is the
length of a sample in minutes: any number under an hour, and from an
hour up a whole number of hours (60, 120, ...), since HYSPLIT reads the
interval in whole hours and a setting such as 90 is refused. A level of 0 counts deposition on the
ground. Every grid samples from the start of the run to its end.

In ``config.yaml``:

.. code-block:: yaml

   models:
     hysplit:
       n_hours: 24
       numpar: 5000
   variants:
     particles: {}
     conc:
       mode: concentration
       pollutants:
         - {name: SO2, rate: 1000.0, hours: 24, deposition_velocity: 0.006}
       grids:
         - {name: local, spacing_lat: 0.01, spacing_lon: 0.01, span_lat: 1, span_lon: 1, levels: [0, 100]}
         - {name: regional, spacing_lat: 0.1, spacing_lon: 0.1, span_lat: 10, span_lon: 10, levels: [500, 3000], sampling_interval: 360}

The ``particles`` variant makes particles as before, and ``conc`` makes
concentration from the same releases. They never share a settings folder.

What a concentration run does not have
--------------------------------------

The particle file's settings (``varsiwant``, ``outdt``, ``veght``) and
``emisshrs`` belong to the particle mode. A concentration run leaves them
out, so a ``hysplit`` block that sets them for its particle variants serves
a concentration variant too; each pollutant has its own ``hours``. A
``pollutants``, ``grids`` or ``conversions`` without ``mode: concentration`` is refused. A stage reads
particles unless its class says another type (its ``result``), so a stage of
particles is refused on a concentration variant; give the variant
``<stage>: null`` under ``stages``. The particle
transforms and the plots of particles are for particles too.
:func:`runtraj.run_trajectories` runs one release in any mode, and returns
the tree of grids in this one.

runtraj does not yet expose time-varying emissions (``EMITIMES``) or
sampling that starts or stops inside the run; open an issue if you need
one.

Other things HYSPLIT can count
------------------------------

HYSPLIT's ``ICHEM`` switches a concentration run to count something else.
It is the setting ``ichem``, 0 unless you set it, and it is part of the
run's settings. We recommend leaving it out until you need one of these:

.. list-table::
   :header-rows: 1
   :widths: 10 45 45

   * - ``ichem``
     - What you get
     - What it needs
   * - 1
     - A grid for each point of the release: which source each cell's
       pollutant came from. The pollutant's variable gets a ``source``
       dimension.
     - One pollutant; a multipoint release (a column release is refused,
       since it is one source).
   * - 10
     - A grid for each hour, or each ``qcycle`` hours, of emission: when
       each cell's pollutant was released. The variable gets a
       ``release_time`` dimension.
     - One pollutant; a forward run; ``qcycle`` in whole hours, which
       ``n_hours`` is a multiple of; ``hours`` of the pollutant no longer
       than ``qcycle``.
   * - 2
     - Pollutants that convert into one another at a rate per hour, such
       as SO2 into sulfate.
     - Two pollutants or more, ``maxdim`` set to their number, and
       ``conversions``.
   * - 6
     - Mass per mass of air in place of mass per volume: the
       concentration divided by the air density at the particle.
     - Nothing.
   * - 9
     - A lowest level that reaches from the ground to a share of the
       mixed layer, wherever each particle is.
     - The first of each grid's ``levels`` as a percentage, 1 to 100, and
       no level of 0.
   * - 5
     - Deposition of whole particles by chance, in place of a share of
       every particle's mass.
     - ``maxdim: 1`` and ``initd: 0``, which are the defaults, and a
       pollutant that deposits.

With ``ichem: 10`` one run tells apart the pollutant of each hour, where
you would otherwise add a release per hour to ``releases.csv`` and run
each:

.. code-block:: yaml

   variants:
     by-hour:
       mode: concentration
       ichem: 10
       qcycle: 1
       n_hours: 24
       pollutants:
         - {name: CH4, rate: 1.0, hours: 1}
       grids:
         - {name: local, spacing_lat: 0.01, spacing_lon: 0.01, levels: [100]}

.. code-block:: python

   ch4 = sim.concentration["local"]["CH4"]  # (release_time, time, z, lat, lon)
   ch4.sel(release_time="2021-07-15 03:30")  # what was released from 03:30
   ch4.sum("release_time")                   # all of it, as ichem 0 counts

Each cycle is released at ``rate`` for ``hours``, so with a rate of 1 the
grids are what one unit of mass per hour in that cycle gives, and you
scale each by the emission you want to test. With ``ichem: 1`` the same
holds for each point of the release, and ``source`` numbers the points
from 1 in their order, with ``source_lon``, ``source_lat`` and
``source_height`` beside it.

Conversions are a list, as pollutants are, each naming two of them:

.. code-block:: yaml

   conc:
     mode: concentration
     ichem: 2
     maxdim: 2
     pollutants:
       - {name: SO2, rate: 1000.0, hours: 24}
       - {name: SO4, rate: 0.0, hours: 24}
     conversions:
       - {source: SO2, product: SO4, rate: 0.01, weight_ratio: 1.5}

``rate`` is the fraction converted per hour, and ``weight_ratio`` the mass
of the product per mass converted (1.5 for SO2 to SO4).

The other values are refused, each with its reason
(:ref:`hysplit-concentration`). ``ichem: 8`` is HYSPLIT's particle output,
which is ``mode: particle``: there ``ichem`` may be 8 or left out, and a
trajectory run reads none.

In a project
------------

A project runs a concentration variant as it runs any other. Each release's
result is one NetCDF file, a group per grid, under ``concentration/`` in
the output directory (:doc:`../reference/layout`). Read them back by
simulation:

.. code-block:: python

   project.status()  # a concentration column, beside particles
   results = project.concentration()  # {(release, variant, realization): DataTree}
   sim = project.simulation(release_id, "conc")
   sim.concentration["local"]["SO2"].sel(z=100).sum("time")

The grids of two releases are centered on each release, so they are not
joined into one array: each simulation keeps its own tree. A grid with a
center of its own is the same for every release, and
:func:`xarray.concat` over ``results`` joins them along a new dimension.
:doc:`../reference/concentration` describes the result and its file.

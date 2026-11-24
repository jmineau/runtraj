Reading And Plotting Particles
==============================

Each simulation's result is its particles: where every particle was at
every output step, one Parquet file per simulation in the output directory.
You can read them for one simulation, for many at once, or with other tools
without runtraj. We plot a few simulations first, because a map shows at a
glance whether the air went where you expect, and only then load many for
analysis.

The examples on this page that run use a small sample project: four
releases an hour apart on the morning of 15 January 2021 at one site in Salt
Lake City, 24-hour backward runs with 100 particles, and one variant,
``hrrr``.

.. This page runs when the docs are built, on a small sample project: four
   releases an hour apart on the morning of 15 January 2021, run on the met
   files the tests cache (docs/_ext/docs_examples.py).

.. ipython:: python
   :suppress:

   import os

   import docs_examples

   docs_cwd = docs_examples.temp_workdir(sample=True)

One simulation
--------------

Open the project, pick a simulation by its release id and variant, and read
its particles:

.. ipython:: python

   import runtraj

   project = runtraj.Project("./my_project")
   sim = project.simulation("202101150800_-111.848_40.766_10", "hrrr")

   sim.has_particles           # True once the particle file exists
   particles = sim.particles   # raises FileNotFoundError before that
   particles.head()

``sim.particles`` is a pandas DataFrame with one row per particle per output
step. The columns you are most likely to use:

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - Column
     - Meaning
   * - ``particle``
     - Particle number, from 1 to ``numpar`` (HYSPLIT's ``indx``)
   * - ``age``
     - Minutes since release, negative for a backward run (HYSPLIT's
       ``time``)
   * - ``time``
     - Time of the row, UTC, built from ``age`` when the table is read
   * - ``lon`` / ``lat``
     - Longitude and latitude (HYSPLIT's ``long`` / ``lati``)
   * - ``zagl``
     - Height above ground, m
   * - ``mlht``, ``sigw``, ...
     - The met variables HYSPLIT writes beside each position, the ones
       listed in its ``varsiwant`` setting, under HYSPLIT's names
       (``mlht`` is the mixed-layer height)

:doc:`../reference/particles` lists every column with its units.

Plots
-----

runtraj's methods for a particle table are under ``particles.runtraj``.
Plotting needs the ``visualization`` extra; with cartopy installed, maps
also show coastlines and state borders.

.. ipython:: python

   @savefig particles_map.png width=6in
   particles.runtraj.plot.map(release=sim.release);   # every position, by age

``color_by="zagl"`` colors the particles by height instead. Two more plots
help before a batch: ``sim.release.plot.map()`` shows where a release is, and
``project.plot.availability()`` the days each location has releases.

.. ipython:: python

   @savefig particles_availability.png width=6in
   project.plot.availability();

A map takes a ``tiler``, any cartopy tiler, to draw map tiles under the
data. The maps on these pages are drawn on shaded-relief tiles of the USGS
National Map. cartopy downloads them when the map is drawn, and
``cache=True`` keeps what it downloaded:

.. code-block:: python

   from cartopy.io.img_tiles import GoogleTiles

   usgs = GoogleTiles(
       url="https://basemap.nationalmap.gov/arcgis/rest/services/USGSShadedReliefOnly/MapServer/tile/{z}/{y}/{x}",
       cache=True,
   )
   particles.runtraj.plot.map(tiler=usgs)

Where the particles ended
-------------------------

``particles.runtraj.endpoints()`` gives the last row of each particle: for
a backward run, where the air was at the start of the run; for a forward
run, where it went. A study that samples a background value there needs a
run long enough for the particles to leave the region of its sources
(:doc:`choosing_settings`):

.. ipython:: python

   ends = particles.runtraj.endpoints()
   ends[["particle", "age", "lon", "lat", "zagl"]].describe()

``particles.runtraj.grid_exits(bounds)`` gives where each particle first left
a :class:`~runtraj.Bounds` box, such as the edge of your study area.

Weighting the particles
-----------------------

A particle carries no mass, so a species that is lost on the way, or a
column measurement that counts some heights more than others, is handled by
weighting each row of the particles. A transform multiplies the ``weight``
column, which starts at 1, by the fraction that counts: here, the fraction
of a tracer with a 24-hour lifetime that survives to the release.

.. ipython:: python

   from runtraj.transforms import FirstOrderLifetime, apply_transforms

   weighted = apply_transforms(particles, [FirstOrderLifetime(lifetime_hours=24)])
   weighted.groupby(weighted.age // 60)["weight"].mean().tail()

The built-in transforms are an averaging kernel and pressure weighting for
a column measurement, a first-order lifetime, dry deposition, and
oxidation; :doc:`../reference/transforms` lists their settings. A stage
applies the transforms its settings list when it reads the particles, so
the weights are not stored in a particle file.

Many simulations at once
------------------------

``project.simulations`` is a table with one row per release under each
variant. Its columns are ``release`` and ``variant``, ``realization`` (``0``
to ``N - 1`` for a variant with ``realizations: N``, empty for one that runs
once), ``model``, the release's ``time``, ``kind`` and ``location``, and any
columns of your own in ``releases.csv``, such as a site name:

.. ipython:: python

   sims = project.simulations
   sims

Select rows the way you would in pandas, then hand the selection to the
project:

.. ipython:: python

   morning = sims[
       (sims.variant == "hrrr")
       & sims.time.between("2021-01-15 06:00", "2021-01-15 07:00")   # both ends included
   ]
   both = project.particles(morning)   # one table, with release and variant columns
   both.groupby("release")["particle"].nunique()

   @savefig particles_two_releases.png width=6in
   both.runtraj.plot.map(color_by="zagl");   # every position of both

Leave the selection out to load every simulation. Any table with
``release`` and ``variant`` columns works as a selection, such as your
observations merged with ``sims``, and so does a boolean mask over
``project.simulations``. A simulation whose particles do not exist yet is
left out. ``columns=["particle", "age", "lon", "lat"]`` reads only those
columns, which is faster and takes less memory.

Particle numbers start again in each simulation, so in such a table a
trajectory is its ``release``, ``variant`` and ``realization`` as well as
its ``particle``. ``endpoints()``, ``grid_exits()`` and the transforms read
it that way: each simulation is handled as if it were alone.

.. ipython:: python

   both.runtraj.endpoints().groupby("release").size()

Your own columns of ``releases.csv`` select one site or one campaign. With
a ``site`` column and an ensemble variant named ``hrrr-ens``:

.. code-block:: python

   site = sims[(sims.variant == "hrrr") & (sims.site == "tower-1")]
   ensemble = sims[sims.variant == "hrrr-ens"]   # every realization of every release

To work with each simulation of a selection, look it up by its row:

.. ipython:: python

   for release, variant in morning[["release", "variant"]].itertuples(index=False):
       sim = project.simulation(release, variant)
       print(sim, len(sim.particles))

A run of 1000 particles over 24 hours is about 1.4 million rows, some 250 MB
in memory, so ``project.particles`` suits a selection of tens of
simulations. For thousands, read the ``particles/`` tree with DuckDB or
pyarrow (below). ``sim.particles_path`` is where one file is.

.. ipython:: python
   :suppress:

   os.chdir(docs_cwd)

In other tools
--------------

DuckDB, R and other languages
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The output directory is a set of Parquet files in folders named
``settings=...`` and ``date=...``, which pyarrow, polars, R's ``arrow`` and
DuckDB read as columns. Each file also has a
``release`` column. With `DuckDB <https://duckdb.org>`_, from Python, R,
or its own shell, a question about thousands of simulations is one query:

.. code-block:: sql

   -- simulations per settings folder and day
   SELECT settings, date, count(DISTINCT release) AS simulations
   FROM read_parquet('output/particles/*/*/*.parquet', hive_partitioning = true)
   GROUP BY settings, date
   ORDER BY settings, date;

   -- where each particle of one day's releases ended up
   SELECT release, particle, arg_max(lon, abs(age)) AS lon, arg_max(lat, abs(age)) AS lat
   FROM read_parquet('output/particles/settings=hrrr-a3f9c2/date=2021-01-15/*.parquet', hive_partitioning = true)
   GROUP BY release, particle;

   -- the time of each row, UTC: the release id starts with the release time
   SELECT *, strptime(left(release, 12), '%Y%m%d%H%M') + to_minutes(age::INTEGER) AS time
   FROM read_parquet('output/particles/settings=hrrr-a3f9c2/date=2021-01-15/*.parquet');

In R, the ``arrow`` package opens the same tree:

.. code-block:: r

   library(arrow)
   particles <- open_dataset("output/particles")   # settings and date are columns

A file stores no ``time``; ``age`` is minutes from the release time, which
the release id begins with and the file's metadata holds. An ensemble's folders have a ``realization=k``
level as well, so its files are one folder deeper (``*/*/*/*.parquet``)
and ``realization`` is a column. :doc:`../reference/layout` describes every
file and metadata key, for a reader in another language.

One file without a project
~~~~~~~~~~~~~~~~~~~~~~~~~~

:func:`runtraj.read_particles` reads one particle file, and
:func:`runtraj.particles.particles_metadata` what it records:

.. code-block:: python

   path = (
       "output/particles/settings=hrrr-a3f9c2/date=2021-01-15/"
       "202101150800_-111.848_40.766_10.parquet"
   )
   particles = runtraj.read_particles(path)
   meta = runtraj.particles.particles_metadata(path)
   meta.release, meta.settings, meta.met_files, meta.realization

The file holds its release, the met files the run read, and the settings it
ran with, the same ones as its folder's ``_settings.yaml``. ``realization``
is which realization of an ensemble the file is, or ``None`` for a single
run.

As CF trajectories
~~~~~~~~~~~~~~~~~~

``particles.runtraj.to_cf_trajectories()`` returns the particles as an
:class:`xarray.Dataset` that follows the CF-1.10 conventions for
trajectories, which `trajan <https://opendrift.github.io/trajan/>`_,
cf-xarray, and other tools that read CF trajectory files open. Each particle
is one trajectory, with dimensions ``(trajectory, obs)``. The coordinates
are ``trajectory`` (the particle number), ``time`` (UTC), ``lon``, ``lat``
and ``zagl``; ``age`` and every other numeric column are data variables. A
particle that leaves the meteorology domain early is padded with missing
values. Write it with :meth:`xarray.Dataset.to_netcdf`:

.. code-block:: python

   import trajan  # registers ds.traj
   import xarray as xr

   particles.runtraj.to_cf_trajectories().to_netcdf("trajectories.nc")
   ds = xr.open_dataset("trajectories.nc", decode_timedelta=False)
   ds.traj.plot()

``decode_timedelta=False`` keeps ``age`` in minutes rather than reading it
as a duration. A table with several realizations is refused: select one
first, ``particles[particles.realization == k]``. The particles of
:func:`runtraj.run_trajectories` convert the same way.

When the particles look wrong, :doc:`checking` lists what to check, in
order.

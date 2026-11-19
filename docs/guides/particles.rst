Reading And Plotting Particles
==============================

Each simulation's result is its particles: where every particle was at
every output step, one Parquet file per simulation in the output directory.
You can read them for one simulation, for many at once, or with other tools
without runtraj. We plot a few simulations first, because a map shows at a
glance whether the air went where you expect, and only then load many for
analysis.

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
   * - ``mlht``, ``foot``, ...
     - The variables in ``varsiwant``, under HYSPLIT's names

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

``particles.runtraj.endpoints()`` gives the last row of each particle. For a
backward run that is where the air came from, which is where to sample a
background value; for a forward run it is where the air went:

.. ipython:: python

   ends = particles.runtraj.endpoints()
   ends[["particle", "age", "lon", "lat", "zagl"]].describe()

``particles.runtraj.grid_exits(grid)`` gives where each particle first left a
:class:`~runtraj.Grid`, for a value sampled at a domain's edge.

Many simulations at once
------------------------

``project.simulations`` is a table with one row per release under each
variant. Its columns are ``release`` and ``variant``, ``realization`` (``0``
to ``N - 1`` for a variant with ``realizations: N``, empty for one that runs
once), ``model``, and then every column of ``releases.csv``:

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

Leave the selection out to load every simulation. Any table with
``release`` and ``variant`` columns works as a selection, such as your
observations merged with ``sims``, and so does a boolean mask over
``project.simulations``. A simulation whose particles do not exist yet is
left out. ``columns=["particle", "age", "lon", "lat"]`` reads only those
columns, which is faster and takes less memory.

The extra columns of ``releases.csv`` select one site or one campaign:

.. code-block:: python

   site = sims[(sims.variant == "hrrr") & (sims.site == "WBB")]
   ensemble = sims[sims.variant == "hrrr-err"]   # every realization of every release

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

In DuckDB and other tools
-------------------------

The output directory is a set of Parquet files in folders named
``settings=...`` and ``date=...``, which pyarrow, polars, R's ``arrow`` and
`DuckDB <https://duckdb.org>`_ read as columns. Each file also has a
``release`` column. With DuckDB, from Python, R, or its own shell, a
question about thousands of simulations is one query:

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

A file stores no ``time``; ``age`` is minutes from the release time, which
the file's metadata holds. An ensemble's folders have a ``realization=k``
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

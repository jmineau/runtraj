FLEXPART Integration
====================

A variant with ``model: flexpart`` runs `FLEXPART 11
<https://www.flexpart.eu>`_ instead of HYSPLIT. The worker gets it as
:class:`runtraj.models.flexpart.FlexpartModel` (see :doc:`execution`), which
picks the GRIB files a release needs, writes FLEXPART's input files, runs
the executable you built, and reads the particles it writes.

.. code-block:: yaml

   models:
     flexpart:
       directory: /path/to/flexpart
       n_hours: -24
       numpar: 1000
   mets:
     era5: /path/to/era5

FLEXPART support is new, and two things about FLEXPART 11 itself limit
what you can use it for today: it runs on ERA5 only, and a run with
several threads does not repeat exactly. Read `Meteorology`_ and
`Repeating a run`_ before you rely on a result.

The FLEXPART build
------------------

runtraj includes no FLEXPART, which is GPL-3. Build it from its source as
its own documentation describes, and set ``directory`` to the checkout:
runtraj runs ``src/FLEXPART`` (``make eta=no``), or ``src/FLEXPART_ETA``
(``make eta=yes``) with ``eta: true``, and reads the land-use, surface and
species files from the checkout's ``options/`` folder.

The SHA-256 of the executable is part of each run's settings, so a rebuilt
FLEXPART gets a new settings folder. The checkout's version is recorded
beside it: a ``version`` file in the checkout if you put one there, else
its git commit.

Install the ``flexpart`` extra (``pip install "runtraj[flexpart]"``). It
adds eccodes, which reads the valid time of each GRIB file, and cfgrib,
which opens them for :func:`runtraj.sampling.sample_met`.

Meteorology
-----------

A met for FLEXPART is a directory of GRIB files, one for each time. runtraj
reads each file's valid time from its first message, so the files can have
any names, except that a name cannot hold a space: FLEXPART reads a name up
to its first space. Other files in the directory, such as the index cfgrib
writes beside a GRIB file, are left out.

You don't say which weather model the files come from: runtraj reads it
from the first file's first message, as it reads an ARL file's header for
HYSPLIT. A GRIB message names the center that made it (``centre``), and
ECMWF's name their MARS class too.

ERA5 (``centre`` ``ecmf``, class ``ea``)
   ECMWF's ERA5 on model levels, as `flex_extract
   <https://flexpart.img.univie.ac.at/flexextract/index.html>`_ writes it.
   Both builds read it. We run it with ``eta: true``, the build FLEXPART's
   documentation intends for ECMWF data, which keeps particles on the
   model's own levels. flex_extract writes three precipitation fields,
   which FLEXPART reads only when it was built with ``numpf = 3`` in
   ``src/par_mod.f90``.

GFS (``centre`` ``kwbc``) is refused, because FLEXPART 11 does not mix its particles
on GFS. NCEP's ``pgrb2`` files hold no surface fluxes, so FLEXPART derives
the surface heat flux and stress from the 2 m and 10 m values against its
lowest level, which GFS diagnoses from that same layer. At a mountain site
at midday they came out near zero (0 W m\ :sup:`-2` where ERA5 has -228),
and particles released near the ground stayed at a median 8 m in a 1000 m
mixed layer. GFS comes back once FLEXPART reads GFS's own fluxes. Any
other GRIB, such as ECMWF's operational analysis (class ``od``), is refused
too, naming what its first file says it is.

``runtraj check`` and ``runtraj fetch`` do nothing for a FLEXPART met yet.
A run still stops before FLEXPART starts when the files do not cover it or
one in between is missing.

What a run does
---------------

FLEXPART keeps each path to 120 characters, so it runs inside the workdir
with relative paths, and the met files it needs are linked into ``met/``
there. runtraj writes ``COMMAND``, ``RELEASES``, ``PARTOPTIONS``,
``OUTGRID``, ``AVAILABLE`` and ``pathnames`` for each release, with
particle output every ``loutstep`` seconds and no gridded output. The
settings in the ``flexpart`` block keep the names they have in ``COMMAND``
(:doc:`configuration`).

A column release is ``numpar`` releases of one particle each, stacked from
the bottom up, and a multipoint release is one release for each point with
the particles shared between them.

FLEXPART ends some failed runs with exit status 0, so a run counts as
complete only when its output ends with FLEXPART's closing line. Its
output is the simulation's log.

Threads
~~~~~~~

Each release runs FLEXPART with ``threads`` OpenMP threads, whatever
``OMP_NUM_THREADS`` is set to. runtraj runs ``cpus`` releases at once, so
ask for ``cpus`` times ``threads`` cores.

FLEXPART runs with its stack limit raised as far as the system allows, as
its documentation asks (``ulimit -s unlimited``). runtraj does not set
``OMP_PLACES`` or ``OMP_PROC_BIND``, which that documentation also
suggests: several FLEXPART processes on one node would each bind to the
same cores.

Repeating a run
~~~~~~~~~~~~~~~

FLEXPART has no seed setting, so ``seed`` is refused. It seeds each
thread's random numbers with a fixed number, which is why ``threads``
is part of a run's settings. Runs with several threads still
differ: two runs with the same build, met and 8 threads gave median
particle heights of 1335 m and 1397 m over the first three hours.

The particles
-------------

:func:`runtraj.models.flexpart.read_partoutput` reads FLEXPART's
``partoutput_*.nc`` files into the particle table (:doc:`particles`), and
reads a run made outside runtraj too. Beside ``lon``, ``lat`` and ``zagl``,
the table has FLEXPART's values at each particle under the names HYSPLIT's
have:

.. list-table::
   :header-rows: 1

   * - Column
     - FLEXPART's name
     - Unit
     - What it is
   * - ``mlht``
     - ``hmix``
     - m
     - Height of the mixed layer above ground.
   * - ``dens``
     - ``rho``
     - kg m\ :sup:`-3`
     - Air density.
   * - ``pres``
     - ``prs``
     - hPa
     - Pressure (FLEXPART writes Pa).
   * - ``temp``
     - ``T``
     - K
     - Temperature.
   * - ``sphu``
     - ``sh``
     - kg kg\ :sup:`-1`
     - Specific humidity.
   * - ``zsfc``
     - ``to``
     - m
     - Height of the terrain above sea level.

Three things differ from a HYSPLIT table:

- **There is no row at** ``age = 0``. FLEXPART skips a particle in the step
  it is released, so a particle's first row is one output step later.
  FLEXPART numbers particles in the order it releases them, so for a column
  or multipoint release ``release_height`` comes from the particle's number:
  particle *k* of a column starts in slab *k*, and a multipoint release's
  points take their particles in turn
  (:meth:`~runtraj.models.flexpart.FlexpartModel.release_rows`).
- **The met columns are NaN at the last output time.** Without gridded
  output FLEXPART writes the positions there but none of the values it
  interpolates from the met. The eta build writes no height there either,
  so ``zagl`` is NaN in a run's last rows.
- **In a run on GFS made outside runtraj,** ``pres`` **and** ``dens``
  **near the ground are those of the lowest pressure level above the
  terrain.** At a site at 1767 m where
  GFS's surface pressure was 817 hPa, FLEXPART's ``pres`` at the ground was
  about 792 hPa.

API
---

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.models.flexpart.FlexpartConfig
   runtraj.models.flexpart.FlexpartMetConfig
   runtraj.models.flexpart.flexpart_version

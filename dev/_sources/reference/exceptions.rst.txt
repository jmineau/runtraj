Exceptions
==========

Every exception runtraj raises is a :class:`runtraj.RuntrajError`, so one
``except`` clause catches them all:

.. code-block:: python

   import runtraj

   try:
       project.run()
   except runtraj.RuntrajError as error:
       print(f"runtraj stopped: {error}")

Each one also subclasses the builtin that describes it. A
:class:`~runtraj.exceptions.SimulationError` is a ``RuntimeError`` and a
:class:`~runtraj.exceptions.HYSPLITNotFoundError` is a ``FileNotFoundError``,
so an ``except`` for the builtin catches it too. Plain input checks, such as
a negative particle count, raise ``ValueError``.

Base class
----------

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.RuntrajError

Failed runs
-----------

A worker records a :class:`~runtraj.exceptions.SimulationError` as a failed
simulation (:attr:`runtraj.Simulation.failure`). Its ``reason`` is a short
name for the cause: for HYSPLIT, one of
:class:`runtraj.transport.hysplit.FailureReason`, such as ``MET_COVERAGE``,
``NO_PARTICLE_DATA``, or ``TIMEOUT``. A run that the meteorology does not
cover fails as ``MET_COVERAGE``.

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.exceptions.SimulationError
   runtraj.exceptions.MeteorologyError

Stopped runs
------------

A run on ``cpus`` above 1 raises :class:`~runtraj.exceptions.WorkerDied` when a
worker process is killed outright, as the out-of-memory killer does. It is not
a failed simulation: nothing is recorded, and running again continues.

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.exceptions.WorkerDied

Setup
-----

A project does not open on a machine where a class its ``config.yaml``
names by import path is not installed
(:class:`~runtraj.exceptions.MissingClass`, an ``ImportError``): a transport
model or a stage in another package. The message names the package to
install, and the ``runtraj`` commands exit with 2.

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.exceptions.HYSPLITNotFoundError
   runtraj.exceptions.MissingClass

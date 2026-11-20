Transforms and Sampling
=======================

.. currentmodule:: runtraj.transforms

Particle transforms say how much each particle row counts: for a column
measurement, its averaging kernel and its share of the column's air; for a
species lost on the way, the fraction that survives to the release. Each
multiplies the particles' ``weight`` column (:doc:`particles`). A transform
is any object with an ``apply(particles, release=None, directory=None)``
method. The built-ins are pydantic models, and their fields are their
``config.yaml`` keys under a stage's ``transforms:``.

.. code-block:: python

   from runtraj.transforms import FirstOrderLifetime, apply_transforms

   weighted = apply_transforms(particles, [FirstOrderLifetime(lifetime_hours=24)])
   weighted["weight"]  # exp(-age / 24 h) at each row

Built-in transforms
-------------------

.. autosummary::
   :toctree: _api
   :nosignatures:

   AveragingKernel
   PressureWeighting
   FirstOrderLifetime
   DryDeposition
   Oxidation

Averaging-kernel tables
-----------------------

.. autosummary::
   :toctree: _api
   :nosignatures:

   averaging_kernel_table

Science helpers
---------------

.. autosummary::
   :toctree: _api
   :nosignatures:

   weigh
   release_coordinate
   particle_pwf
   ak_weights
   survival
   dry_deposition_survival

Loading and dumping
-------------------

.. autosummary::
   :toctree: _api
   :nosignatures:

   load_transform
   dump_transform
   apply_transforms
   transform_kind

Sampling a field at particles
-----------------------------

.. currentmodule:: runtraj.sampling

:func:`sample_field` reads a gridded field (an :class:`xarray.DataArray`
on a ``lat``/``lon`` or ``y``/``x`` grid, with an optional vertical
dimension and ``time``) in the cell nearest each point: an oxidant along
each trajectory, or a mole fraction where each particle ends.

.. autosummary::
   :toctree: _api
   :nosignatures:

   sample_field
   vertical_dim
   nearest_cell

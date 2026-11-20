Project And Output
==================

A project is a folder holding ``config.yaml`` and ``releases.csv``. Its
results go to the output directory that ``config.yaml`` names, one tree per
kind of result with a folder per set of settings (:doc:`layout`). Most
users only need :class:`runtraj.Project`. ``project.output`` is the
:class:`~runtraj.output.Output`.

Project
-------

:class:`runtraj.Project` is documented with the :doc:`core`.

Output directory
----------------

.. autosummary::
   :toctree: _api
   :nosignatures:

   runtraj.output.Output
   runtraj.output.completed

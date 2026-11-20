# Changelog

All notable changes to runtraj are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[PEP 440](https://peps.python.org/pep-0440/). runtraj is alpha software: before
1.0, a minor or pre-release version may change the public API.

## [Unreleased]

## [0.1.0a1] - 2026-11-19

The first release. runtraj runs particle dispersion models at scale: you give
it the times and places particles are released from, the meteorology, and the
settings to run under, and it runs the transport model once for each release,
on your computer or as a Slurm job array, and keeps the particles where you
can find them again.

### Added

- Projects: a folder holding `config.yaml` and `releases.csv`, whose
  simulations are every release under every variant (`runtraj.Project`).
- Releases: `PointRelease`, `ColumnRelease` and `MultiPointRelease`, read from
  and written to `releases.csv`.
- Variants and realizations: several sets of settings in one project, each
  writing to its own settings folder, and repeated runs with different seeds.
- HYSPLIT (`hycs_std`) as the built-in transport model, with its settings in
  a `hysplit` block; runtraj does not include HYSPLIT itself.
- The met catalog: finding the met files each release needs, checking them
  before a run, and downloading NOAA's archives (`runtraj fetch`, with the
  `download` extra).
- Stages, for what is made from the particles after a run, and other
  transport models, both plugged in by the import path of a class.
- Particle transforms for a lifetime, deposition, oxidation, pressure
  weighting and an averaging kernel.
- Met sampling: the met's mixed-layer height, air density, pressure and
  ground height at each particle, read from ARL files as HYSPLIT reads them
  (`runtraj.sampling.sample_met`), and HYSPLIT's mixed-layer height computed
  from the met's profile (`calc_pbl_height`).
- Running: `runtraj run` (or `Project.run()`) on your computer, `runtraj
  submit` as a Slurm job array, and `runtraj status`. A simulation is complete
  when its files exist, so an interrupted run picks up where it stopped.
- `runtraj check`, which finds what would stop a project's runs from its
  inputs alone.
- Output on a local disk or an object store, read back as plain DataFrames
  with the `.runtraj` accessor, and maps with the `visualization` extra.

# Changelog

All notable changes to runtraj are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[PEP 440](https://peps.python.org/pep-0440/). runtraj is alpha software: before
1.0, a minor or pre-release version may change the public API.

## [Unreleased]

### Fixed

- A variant's `realizations` must be a whole number, 1 or more. A float such as `2.5`, a boolean, a string or a list is refused with an error naming the variant and the value, instead of being rounded (`2.5` ran 2 realizations, `true` ran 1) or ending `runtraj check` in a traceback.
- `oxidation` reads its oxidant field's levels in their own units: a field on pressure levels in Pa (or kPa, mb) is matched to a particle's `pres` in hPa, and levels in m or km to `zagl`, instead of picking the level nearest the number. Levels whose `units` cannot be matched to the particle column are an error; levels with no `units` are taken to be in the particles' units, as before.
- `oxidation` refuses particle times more than one step outside its oxidant field's times, instead of giving them the first or last step, which gave every row of a monthly climatology labeled with another year its December value. Relabel a climatology with the dates of the run.
- **Breaking:** `Project.run` and `Project.submit` take every argument by keyword. A release id list passed first was read as `skip_existing`, and the whole project ran or was submitted; it is now a `TypeError`. Pass `releases=` for a selection.

## [0.1.0a5] - 2026-11-24

### Added

- Run HYSPLIT on Windows (`exec/hycs_std.exe`), and test runtraj on windows-latest.
- Run a project as a PBS Pro or OpenPBS job array with `backend: pbs`.
- A backend is a `Backend` class, found by name or by the import path of its class, so a scheduler runtraj does not submit to can be added from another package.
- A guide to running a project on any scheduler: the `--task` contract, GNU parallel and LSF examples, and the exit codes.
- `runtraj.testing.run_stage` runs a stage end to end through a project, on a replay transport model, so a stage in another package can test itself the way a project runs it.
- A stage may set `version`, which is part of the hash of its settings folders, so a fix that changes a stage's files makes new ones. A stage that never sets it keeps its folders.

### Changed

- **Breaking:** a transport model subclasses `runtraj.models.TransportModel` and a stage subclasses `runtraj.stages.Stage`. Each is a base class with every optional hook at its default, so a model or stage in another package writes `run` and sets its attributes. `BatchedTransportModel` is gone (set `batched = True` and override `run_many`), and so are `runtraj.stages.reads` and `result` (use `stage.reads` and `stage.result`).
- **Breaking:** a transport model's met hooks (`settings`, `check`, `fetch`, `downloads`, `open`) are on its met config, a subclass of `runtraj.models.MetConfig`. HYSPLIT's `MetConfig` is now `HysplitMetConfig`.
- **Breaking:** HYSPLIT's met files are found through `arlmet.catalog`, and runtraj's own `Catalog` is gone. This needs arlmet 0.1.0b14. A missing stretch is named by the met steps no file holds. A file cut partway through a time step is not used, two copies of one file fail a run naming both, and so does a short copy beside a longer one. The first run after upgrading reads every met file once to build the catalog (12 minutes on an archive of 16,724 files), and a task then takes about 5 to 9 seconds to list its folder.
- **Breaking:** a Slurm job array has ten tasks per worker. `n_workers` is the most tasks running at once, `n_tasks` is new, and `array_parallelism` is gone. A task that runs out of time has less left when it starts again.
- A stopped Slurm task is requeued by its job script, not by the worker. A stopped worker exits with 3 on every backend.
- The Slurm backend has a module of its own, `runtraj.execution.slurm`.

### Fixed

- Refuse volumes, names, images and delays that Kubernetes would refuse before anything reaches the cluster. `runtraj status` says when the output directory is missing, `runtraj push` warns about files the bucket has that the project no longer does, and `sites.csv` warns on a repeated site.
- A lock is taken and removed when Windows refuses its file for a moment.

## [0.1.0a4] - 2026-11-21

### Added

- Run HYSPLIT's concentration mode with `mode: concentration` and lists of pollutants and grids. Each release stores its concentration grids in one NetCDF file, read with `project.concentration` and `sim.concentration`.
- Set HYSPLIT's `ichem` in a concentration run: a grid for each release point or emission cycle, conversions between pollutants, mixing ratios, and a mixed-layer level.
- Run HYSPLIT's trajectory program with `mode: trajectory`. Each release stores a trajectory table, read with `project.trajectories` and `sim.trajectories`.
- `sim.result` reads what a simulation made, whatever its type, and `run_trajectories` returns what the settings make.
- A stage names the type of result it reads, particles unless it says.
- With `fetch` set in the `kubernetes` block, each pod gets the met files in turn before its share runs (`runtraj run --fetch`).
- A guide to setting up Kubernetes: the image, kind on your computer, Google Cloud and other clusters.

### Changed

- **Breaking:** a transport model's result has a type, particles or concentration. `ModelRun.particles` is now `ModelRun.result`, a model's `particle_files` is now `result_files(config)`, `Variant.particles_hash` is now `Variant.result_hash`, and the worker's `run_particles` is now `run_result`. Settings folders of particles keep their names.

## [0.1.0a3] - 2026-11-20

### Added

- Submit a project to Kubernetes as an indexed Job.
- Run a project every hour on Kubernetes with `runtraj schedule` and `runtraj tick`.
- `runtraj status` lists a Kubernetes project's Jobs and their tasks.
- Choose how `calc_pbl_height` finds the mixed layer, to match the transport model's run.
- `check_model`, the promises a transport model keeps.
- Export `ControlFile`, `read_namelist`, `stored_release` and `release_metadata`, and name the command group `ExitCodeGroup`.

### Changed

- **Breaking:** the transport model refuses releases and says where its particles started.
- Read HYSPLIT's particle file with pandas' C reader, three times faster than `numpy.loadtxt`.
- arlmet is capped below 0.2, so a release that drops a name cannot break a fresh install.

### Fixed

- Keep a failed run's workdir without the transport model's particle file.
- Read a table of several simulations one trajectory at a time.
- Raise `MeteorologyError`, also a `FileNotFoundError`, when a met's files cannot be found.
- Give each local pool worker its share of the CPUs as `OMP_NUM_THREADS`.
- Refuse inputs that gave a plausible wrong value, and name the argument, file and row when an input is refused. Times with a zone are read in UTC.
- Let `PressureWeighting`'s `surface_pressure` close the bottom slab and set the weights.
- Leave a run of a model that writes no log with no log, not runtraj's started line, and document a missing log.
- Name the download option, the HYSPLIT source and the pystilt extra in three error messages.
- Refuse a negative seed, which gave the same run as its positive.

## [0.1.0a2] - 2026-11-19

### Added

- Met sampling from any format arlmet converts, such as GFS and ERA5 GRIB:
  `sample_met` and `calc_pbl_height` take a Dataset in
  `arlmet.open_dataset`'s layout in place of ARL files. A transport model
  whose met files are not ARL files opens them for this with an optional
  `open_met` method, which `runtraj.models.open_met` calls.

### Changed

- runtraj requires arlmet 0.1.0b12 or later.

### Fixed

- Met sampling takes pressure from the met's vertical levels where the met
  has no `PRES` field, and refuses terrain-following and WRF met without it.
- Particle times that are not whole microseconds are sampled at their own
  time.
- Air density is the dry-air density where the met has no `SPHU`.
- `calc_pbl_height` gives no mixed-layer height where the met lacks the
  surface fluxes (`SHTF`, `USTR`); between a met time without them and one
  with them, it takes the height that exists.
- Met files that do not all hold the same variables are sampled for the
  ones they share, and one error names every variable that is missing. No
  met files at all is a clear error.

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

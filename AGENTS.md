# AGENTS.md

Orientation for contributors and coding agents working on runtraj: the
architecture, the invariants that must not break, and the development
workflow. User docs: `docs/` and <https://jmineau.github.io/runtraj>. Longer
references: [CONTRIBUTING.md](CONTRIBUTING.md) (setup, checks, recipes,
building, releasing) and [docs/development/architecture.rst](docs/development/architecture.rst).

> **Keep this file current.** If you change the module layout, the build/test
> commands, or learn a new invariant or gotcha, update the matching section in
> the same change. A section that no longer matches the code is worse than no
> section: fix it or delete it.
>
> Personal or machine-specific notes (local paths, cluster setup) belong in an
> untracked file, not here. Anything matching `*.local.md` is gitignored for
> this (e.g. `CLAUDE.local.md`); add your tool's local files to `.gitignore` if
> they are not covered. Some agents stop reading `AGENTS.md` once a local
> instruction file exists, so import or reference it from yours.

## Overview

runtraj runs particle dispersion models at scale: projects of releases and
variants, settings folders, completion by file, `run` and `submit`,
realizations, the met catalog, and the transport models. It is not a model.
runtraj supports **HYSPLIT** and **FLEXPART 11** (the user installs the model), and there is no default
model: `config.yaml` names one in its `models` block; any other model, and what is made from the
particles after a run (a **stage**), plug in by the import path of a class.
It is alpha software: **there are no backward-compatibility guarantees
before v1.0**, so prefer the clean design over a compatibility shim.

- **A stage in another package is only ever an example here.** runtraj
  imports no stage and names none in its code; where the docs need one to
  show, it is PYSTILT's footprint (`stilt.footprint.FootprintStage`),
  and nothing else of that package is named.
- **A change that breaks a plug-in** (the stage or transport model
  protocols, or the output layout) raises the minor version: a package
  built on runtraj requires a range of it up to the next one.
- **Release** is the time and place a run starts from: `releases.csv`, the
  `release` column, `--releases`, `group`, and the classes `PointRelease`,
  `ColumnRelease`, `MultiPointRelease`. A release placed at a measurement and
  followed backward is often called a receptor.
- **Particle columns** get plain names for HYSPLIT's `indx`, `time`, `long`,
  `lati` (`docs/reference/particles.rst`).
- **Config** is what the user writes, always a class next to the code that
  uses it (`ProjectConfig`, `HysplitConfig`, `ExecutionConfig`, ...).
  **Settings** are what a result was made with, as recorded; never a class.

## Architecture

**There is no index, manifest, or registry.** A project is a directory of
inputs (`Project`: `config.yaml`, `releases.csv`) and an output directory
(`Output`) that `config.yaml` names and several projects can share. Its
simulations are **releases × variants** (at least one variant). Each `Variant` (`runtraj.config`) has
run settings, whose hash names the folder of its model's result (its
particles folder, for most), and the settings of each
stage it uses, hashed with what the stage reads (`runtraj.identity`); variants
with equal run settings share one run per release, and a changed setting
hashes to a new folder (`docs/reference/layout.rst`). **A stage** makes one
file per release after the model runs. `config.yaml` names its class by
import path, in a block named for the stage, and the class subclasses
`runtraj.stages.Stage`, so the core handles any stage and imports none
(`CONTRIBUTING.md`, "Adding a stage").

**A model's result has a type** (`runtraj.results`): `particles` (the
particle table, Parquet), `concentration` (a `DataTree` of grids, NetCDF;
HYSPLIT's `mode: concentration`) or `trajectories` (the trajectory table,
Parquet; HYSPLIT's `mode: trajectory`, which runs `hyts_std`). A model's optional `result(config)` names
it (`particles` when it does not), `Variant.result` holds it, and it names
the tree its files are in; models that make one type follow its one
schema. A stage reads one type, its optional `result` (`particles` when it does not say), and a variant of another type refuses it; its folder's hash is keyed by that type. A
particles record never names its type, so no stored hash moved when types
came in.

**`Project` reads; the workers write results.** `Project` writes only its
inputs (`config.yaml` once, at `Project.init`; `add_releases` appends to
`releases.csv`; `add_table` writes `tables/<name>.parquet`) and knows no
scheduler or compute root; `runtraj.execution.run` and `submit` start the
workers. **Tables are plain DataFrames; the verbs are on `Project`**: a
selection of `project.simulations` (any table with `release` and `variant`
columns, or a mask) goes to `status`, `incomplete`, or `particles`. Do not
wrap the table in a class again. `Output` has no folder classes: its methods
take the kind (`particles` or a stage's name). `runtraj.__all__` (and each
subpackage's) is the public surface.

**A package that adds a stage may subclass `Project` and `Simulation`** with
the reads of what its stage makes. Workers and stages get
runtraj's classes; nothing on disk names a subclass. A plug-in imports only
names in runtraj's `__all__`s and copies any helper it wants
(`tests/test_stages.py` holds the toy stages to it).

```
src/runtraj/
  cli.py          the runtraj command (Typer)
  project.py      Project: inputs, the simulations table, run/submit, status
  check.py        check_project: what would stop a project's runs, from its inputs
  fetch.py        fetch_project: gets the met files the unfinished simulations need
  output.py       Output: the output directory; `completed`, the completion rule
  simulation.py   Simulation: a frozen (release, variant, output) value
  config.py       ProjectConfig and Variant;  identity.py  settings records, hashes
  stages.py       the Stage base class, get_stage, check_stage;  _plugins.py  a class by import path
  testing.py      run_stage: a stage end to end in a project, on the replay transport model
  releases/       release classes, releases.csv, shared validation
  results.py      the result types (particles, concentration, trajectories): suffix, check, write, read
  particles/      the particle table and files, release heights, the particles.runtraj accessor
  concentration.py  the concentration result: a DataTree of grids, its NetCDF file
  trajectories.py   the trajectory table and its Parquet file
  models/         TransportModel/TransportConfig base classes, MODELS (hysplit, flexpart), get_model, run_model, check_model
    hysplit/      config (and its mode), met, driver, CONTROL and SETUP.CFG writers, cdump and tdump readers, failures
    flexpart/     config, GRIB met catalog, COMMAND/RELEASES/... writers, driver, partoutput reader
  execution/      ExecutionConfig (a block per backend), backends (Backend, get_backend: a backend by name or import path),
                  runner (plan, local run), slurm (job array; its script requeues a stopped task), pbs (job array), worker,
                  kubernetes (indexed Job, hourly CronJob), inputs (bucket copies, lock), schedule (hourly releases)
  transforms/     particle transforms: each multiplies the `weight` column, one module each
  sampling.py     sample_field: a gridded field read at points
  meteorology.py, spatial.py, visualization.py, exceptions.py, _paths.py,
  _process.py     a transport model's program in a session of its own, stopped and watched
```

**A run** (`runtraj run`, `Project.run()`, `submit()`) happens where its
backend says (`execution.backend`, a `Backend` found by `get_backend`): in this
process, or as tasks (a Slurm job array, a Kubernetes indexed Job) that each
run `runtraj run --task i/N`. A backend only starts tasks and, if it can, waits;
the branches on its name live in its class, not in `cli.py` or `runner.py`. The unit
of work is a release: the worker runs the model once per run-settings hash,
then each missing stage file of the variants that share the particles, a
stage after the ones it `reads` and again when one of those was remade
(`run_release`, `run_stages`); a batched model
(`batched = True`) first runs a variant group's releases in one call.

The import contracts in `pyproject.toml` (`just imports`) hold this shape: an
exhaustive `layers` contract (a new top-level module fails until placed), only
`runtraj.models` imports a model's package (`runtraj.models.<name>`, such as
HYSPLIT's), no model's package imports another's, and the project config reads
no results and runs nothing. Slurm tasks rebuild the project in another process,
so anything a worker needs must be in the project or the output directory.

`ProjectConfig` is `config.yaml`'s blocks, and no setting sits outside one: `mets`,
`models` (required: no default model), `stages`, `variants`.
Each part owns its config class; the base `TransportConfig` gets a parameter only
when the core reads it. Config classes do no I/O when they validate; `resolve` asks
each stage to read the files its settings name, and the model its version.
`HysplitConfig` has HYSPLIT's own defaults (`n_hours` none).

## Invariants

- **Completion is by file.** A simulation is complete when the model's
  output file exists, and so does the file of every stage its variant uses;
  `runtraj.output.completed` is the one rule, over folder listings. Never add
  another, a completion registry, or a manifest.
- **Bulk methods work from listings** and release ids, never a `Simulation`
  per row.
- **One code path for a disk and an object store, no store class.**
  `Output.directory` is a `Path` (local fast path) or a `UPath` (`path.fs`);
  see `runtraj._paths`. A local output must stay as fast: time a large listing.
- **Identity is content.** runtraj never deletes or overwrites a results folder.
- **The CLI stays thin.** Orchestration does not belong in `cli.py`.
- **ARL met I/O goes through [arlmet](https://github.com/jmineau/arl-met)**; FLEXPART's GRIB, through eccodes (valid times) and cfgrib with arlmet's converters (`FlexpartMetConfig.open`).
- **runtraj ships no HYSPLIT file**: `directory/exec/hycs_std` and `bdyfiles/`, else `PATH`. The trajectory mode runs `exec/hyts_std`, found and hashed the same way. The SHA-256
  of `hycs_std` is in the particles hash, read on first use (`Variant.build`); its
  printed version is recorded as `model_version`, never hashed. Without `hycs_std` a
  project opens and `Output` compares on `Variant.readable_run_settings`.
- **Pydantic for all configuration**: every field a plain
  `Field(default, description=...)`, and the settings inside a block stay flat:
  no nested blocks. The one exception is a list of records of one kind that
  the model itself takes as a list (HYSPLIT's `pollutants` and `grids`), each
  record flat in turn.

## Development

Driven by [`just`](https://github.com/casey/just) and
[`uv`](https://docs.astral.sh/uv/); `just` alone lists every recipe. CI
(`.github/workflows/`) runs the same recipes. The tooling comes from
[jmineau/python-template](https://github.com/jmineau/python-template)
(`.copier-answers.yml`; `copier update` pulls in its changes).

| Command | What it does |
|---|---|
| `just sync` | `uv sync`: the package and the dev tools into `.venv` |
| `just test` | the unit tests and the `>>>` examples under `src/`, in parallel (extra args go to pytest; `-n 0` for serial); `just sync-floors` first runs them with every dependency at its minimum |
| `just lint` / `just format` | ruff check and format check / fix and format |
| `just type-check` | pyrefly, against Python 3.11 (set in `pyproject.toml`) |
| `just imports` / `just docstr` | the import contracts / docstring coverage of the public API (95%) |
| `just quality-check` | lint, type check, import contracts, docstrings, unit tests |
| `just pre-commit` | every pre-commit hook on every file |
| `just build-docs` | a clean Sphinx build that runs every example; warnings are errors |
| `just dist` | the sdist and the wheel, checked with twine |

### Tests

`just test` runs every test without the `integration` marker. Warnings
are errors (a test that expects one uses `pytest.warns`). CI runs `-m integration`
in `tests.yml`:

- `integration`: end to end with real met files and HYSPLIT. Needs a `hycs_std` on
  `PATH` (unit tests get a stand-in) and `RUNTRAJ_TEST_MET_DIR` (HRRR ARL files) or
  `RUNTRAJ_TEST_FETCH_MET=1` (downloads seven 6 h blocks, 23.9 GB in all, and keeps
  211 MB in the ignored `tests/met_cache/`); otherwise they skip.
  The trajectory mode's also need a `hyts_std` on `PATH` (NOAA's public build has one) and skip without.
  CI has no HYSPLIT: it replays the runs recorded in `tests/recorded/hysplit/<version>/`,
  and the docs build replays those of 5.1.0 (`just replay-folder`)
  (`just test-replay 5.1.0`, `5.4.2`; `tests/fixtures/replay.py`). A change to what
  runtraj gives HYSPLIT fails there until someone with the builds runs `just record`
  (CONTRIBUTING.md, "Testing against real HYSPLIT").
- `tests/kubernetes/`: the end-to-end test on a kind cluster, run by `kubernetes.yml`
  only (it needs Docker); its README says what it checks. Its HYSPLIT replays the
  runs in `tests/recorded/kubernetes/5.1.0/` (`just record-kubernetes`).

Docstring examples (`>>>` lines under `src/`) run as tests too
(`--doctest-modules`), so an example that stops matching the code fails
`just test`. One that needs a HYSPLIT run, met files or the network gets
`# doctest: +SKIP` on its `>>>` line.

pytest loads a local `.env` for such variables. Test folders follow the
package. The toy stages in `tests/fixtures/stages.py` stand for a stage in
another package; a test imports no other. The toy transport model in
`tests/fixtures/models.py` is made unlike HYSPLIT (no executable, workdir,
log, met files or `age = 0` row; particles numbered 10, 20, ...):
`tests/test_toy_model.py` runs the core on it. Never commit met files.

### Code conventions

- Keep changes small; don't bundle unrelated cleanups into a fix. Every
  behavior change gets a test, every bug fix a test that fails without it.
- Prefer code that is easy to read and debug (users are scientists who read
  the source when a result looks wrong) over micro-optimizations, unless a
  profile shows the cost; HYSPLIT and I/O dominate real runs.
- **Windows is tested** (`windows-latest`, unit and recorded-run tests). Text
  files are read and written with `encoding="utf-8"` (Windows' default is
  cp1252), HYSPLIT's input files with `newline="\n"` and `as_posix()` paths, so
  they are the same bytes everywhere. A HYSPLIT program is `hycs_std.exe` there
  (`driver.program_file`); stand-ins are `.cmd` files (`program_path` in
  `tests/fixtures/factories.py`). A test that needs a shell script, a POSIX
  signal or process group, or Slurm gets `@not_on_windows("why")`
  (`tests/fixtures/platform.py`). The input lock and `put` on a local folder
  are for Kubernetes and Slurm, which run Linux; from Windows a Kubernetes
  submit needs `inputs` on a bucket (`_pods_open` refuses it otherwise).
  Windows refuses a rename onto a file another process has open, so a file
  that workers write once and then read (`_settings.yaml`) is written with
  `atomic_path(path, replace=False)`, which keeps the file already there; a
  short met crop that cannot be replaced gets a hidden sibling (`met._spare`).
- Python 3.11+ (use `typing.Self`, `enum.StrEnum`, `datetime.UTC`, not
  backports). Ruff rules are in `pyproject.toml`; **NumPy-style docstrings**
  with the summary on the line after the opening quotes (`D213`). Keep a
  module's `from __future__ import annotations` header where it has one.
- pyrefly must pass on `src/`. Fix types at the source, not with
  `typing.cast`; for an error in a library's stubs (often pandas-stubs), put
  `# pyrefly: ignore[<code>]` on the line above, after a comment saying why.
- Exceptions live in `runtraj/exceptions.py`, each subclassing `RuntrajError` and
  the builtin that describes it; a failed run is a `SimulationError`. Plain
  input checks raise builtins such as `ValueError`.
- Extras: `visualization`, `download`, `concentration` (netCDF4), and `complete`, which the `dev` group installs.

### Documentation

A user-facing change updates the docs in the same change: the guide when
what the user does changes, the reference (docstring Notes) otherwise. A bug
fix's edge case goes to the reference. `just build-docs` must pass. Examples run when the docs build, so
they use synthetic data, a small repository file, a public download, or the
test met cache, and seed their random numbers; an `.. ipython::` block has no multi-line statement as its 10th or 100th (`CONTRIBUTING.md`). Give
every public property and attribute a docstring, with no colon in the middle
of its first line (napoleon reads `text: more` as `type: description`).

**Voice** (docs, docstrings, field descriptions, CLI help): write for
scientists who want to run the model and trust the result. Open with the
reader's goal, recommend a default and say why ("you" for the reader, "we"
for our advice), and show real output. One point per paragraph; no
metaphors, selling, or filler; American spelling. The full guide, with the habits that read as
machine-written, is "Writing documentation" in `CONTRIBUTING.md`. Words:

| Say (user-facing) | Not |
|---|---|
| settings folder | key, hash, identity |
| transport model (in full) | model, when the weather model or a modeled value is near |
| particles (the table); run (the verb); trajectory (one particle's path) | run as a noun for the result |
| workdir (one simulation's folder); compute root (where workdirs are made) | scratch, except the output's `scratch/` folder |
| log, met files | provenance |
| release id; simulation (a release under a variant) | simulation ID as a folder |
| the settings of a variant | resolve, record, `model_copy`, frame, handle |

### Commits, releases, issues

- Conventional Commits (`fix(slurm): ...`, `docs: ...`). A pull request leaves
  `CHANGELOG.md` alone: at release, `just changelog` drafts the entries from the
  commit messages (`cliff.toml`) and the maintainer edits them in, so write each
  message as the line a user would read. The version comes from git tags
  (setuptools-scm). Releases are cut by the maintainer (CONTRIBUTING.md).
- `uv.lock` is updated by the Upgrade lock workflow (`lock-upgrade.yml`, monthly,
  opens the `chore/lock-upgrade` pull request), not by Dependabot.
- Open work is in [GitHub issues](https://github.com/jmineau/runtraj/issues);
  read one before working on it. Over-engineered code goes on the
  issue labeled `simplify`. A pull request fixes one thing and links its issue.

## Gotchas

- **Results are plain data**: `sim.particles` is a DataFrame, with the
  `.runtraj` accessor; no wrapper classes. A missing result raises
  `FileNotFoundError` (not cached); test with `has_particles`.
- **`project.simulations` is built once and copied** on each access; a
  hand-edited `config.yaml` needs a new `Project`.
- **A failure record is a note** (`<release id>.failure.yaml` in the logs, its
  `stage` `particles` or a stage's name), removed when the result is written
  and never read by completion.
- **A stage's `config_class` reads back what its `settings` wrote**: a stored
  folder is found that way (`runtraj.stages.check_stage` in the stage's tests).
  A stage writes its file under the temporary path `run` is given; its old
  file is removed first, so a failed remake leaves none.
- **Met that ran out is a failure; a domain exit is not.** HYSPLIT's warnings
  are unreliable, so `Met.files_for` and `Met.check` stop a run on missing or
  damaged met before it starts (`FAILURE_PHRASES` maps the log afterwards),
  and `check_met_stop` fails a run that stopped early with a particle more
  than 5 grid cells inside the domain (only at `outdt: 0`).
  `runtraj check` runs the file search for every release early; it opens one
  met file per met, to count its time periods (HYSPLIT stops on a file with
  one, and `Met.check` refuses it), and one per grid arlmet's catalog lists, for
  the domain; so a damaged file still fails at run time.
- **A `cdump` does not say which `ICHEM` wrote it.** `read_cdump` is told
  the run's `ichem` and `cmass`; 1 and 10 are read as a `source` or
  `release_time` dimension. Each rule of `_validate_ichem` is something a
  run of HYSPLIT 5.4.2 showed (`docs/reference/hysplit.rst`).
- **`hyts_std` reads the dispersion entries of `SETUP.CFG`** (`ICHEM` and
  `IDSP` change a trajectory), so a trajectory run writes only
  `TRAJECTORY_SETUP_SETTINGS`, and records only `TRAJECTORY_SETTINGS`. It
  starts a trajectory at every `CONTROL` location (no line source: a column
  release is refused), and drops a location outside the met's domain and
  numbers the rest from 1, which fails the run.
- **FLEXPART ends some failed runs with exit status 0** (a Fortran `STOP`), so `run_flexpart` also
  needs its closing line. Its settings are refused where FLEXPART would change them itself (`cblflag`).
- **Realizations are an axis**: `realizations: N` is one variant with a
  `realization` column and `realization=k/` partitions, run with `seed + k`.
  HYSPLIT gets `SEED = -(|seed| + 1)` (`setup_seed`), and `seed` needs `krand: 2`.
- **HYSPLIT chains same-location starts into a line source**: that is how
  `ColumnRelease` works, and why HYSPLIT refuses a `MultiPointRelease` that
  repeats a horizontal location (its `check_releases`, which `runtraj check`
  lists and `run_model` fails as `RELEASE_REFUSED` before the model runs).
  The core refuses only two points at one location, which `releases.csv`
  reads as a column.
- **HYSPLIT stops releasing at `MAXPAR`** and fills a multipoint release's
  locations in order. An unset `maxpar` is chosen per release
  (`HysplitConfig.maxpar_for`), so it is recorded unset; `check_release` fails
  a multipoint run whose `WARNING` says `exceeding puff limit` (a one-location
  run writes it too, harmlessly).
- **Release heights come from the `age = 0` row** (`add_release_heights`), or
  from the model's `release_rows(particles, release, config)` when it writes
  none, as HYSPLIT 5.1.0 to 5.4.2 do: a column there is `particles_per_step`
  particles up the column each minute of `emisshrs`, numbered in order
  (`models/hysplit/releases.py`). With neither, a column or multipoint run
  fails `NO_RELEASE_ROWS`.
- **Never name a workdir from the simulation alone**: each run gets a new path
  in the compute root (`_new_workdir`), so projects can share one. Real runs
  are heavy: on a shared HPC system use Slurm or an allocation.
- **The pool is a `ProcessPoolExecutor`** holding at most `cpus` releases, so
  the ones it holds are in progress. A worker killed outright breaks it:
  `run_releases` raises `WorkerDied` naming them, and `runtraj run` exits with 3.
  Workers must exit on SIGTERM: the executor's loop catches `SystemExit`, so
  `_pool_run` ends the process itself, or a stop waits for ever. Windows has
  no SIGTERM: `terminate` ends a worker alone and its HYSPLIT runs on, so
  `_end_worker` ends the tree with `taskkill /T`. A worker killed outright
  runs no `finally`, so HYSPLIT or FLEXPART ends with it by other means
  (`_process.ended_if_this_process_dies`): a watchdog `sh` on POSIX, started
  before the model and sent its group, which kills the group when the worker's
  pipe to it closes (`kill -9 -PGID`, the form dash reads too), and a job
  object that ends on close on Windows.
- **A pool with `cpus` above 1 first starts one worker that does nothing**
  (`_check_workers_can_start`), unless the start method is `fork`. Under `spawn`
  and `forkserver` a worker runs the user's script again, and a script without
  `if __name__ == "__main__":` fails in every worker; the pool would end with
  `WorkerDied` ("out of memory") or a bare connection error. The check shows
  the guard instead. Tests that fake the pool patch it out.

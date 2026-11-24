# Contributing to runtraj

Bug reports, documentation fixes, and code are all welcome. This page covers
how to set up a checkout, the checks a change has to pass, how to write the
docs, and how to add the most common kinds of extension.
[AGENTS.md](AGENTS.md) describes the architecture and the rules the code
relies on.

## Getting started

1. Fork the repository on GitHub and clone your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/runtraj.git
   cd runtraj
   ```
2. Install the package and the development tools into `.venv` with uv:
   ```bash
   uv sync
   ```
3. Install the pre-commit hooks:
   ```bash
   uv run pre-commit install
   ```
4. The unit tests need no HYSPLIT: they put a stand-in first on `PATH`.
   The tests that run HYSPLIT (`integration`) need the test met files, and
   either a HYSPLIT folder's `exec/` on `PATH` (with its data tables in
   `bdyfiles/` beside it) or the runs recorded in this repository:
   `just test-replay 5.4.2` needs no HYSPLIT
   ([Testing against real HYSPLIT](#testing-against-real-hysplit)). Without
   either, those tests are skipped. The docs build needs a HYSPLIT folder
   on `PATH` too: `just replay-folder 5.1.0 DEST` makes one that replays the
   recorded runs.

## Making a change

1. Create a branch:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. Make your change. New behavior needs a test, and a bug fix needs a test
   that fails without the fix. User-facing changes need a docs update.
3. Run the checks:
   ```bash
   just quality-check   # ruff, pyrefly, the import contracts, docstrings, and the unit tests (with the docstring examples)
   just pre-commit      # all pre-commit hooks
   just sync-floors     # optional, then `just test`: every dependency at its minimum, as CI's "minimum dependencies" job does
   ```
4. Commit with a [Conventional Commits](https://www.conventionalcommits.org/)
   message (`fix(slurm): ...`, `docs: ...`), push to your fork, and open a
   pull request. Leave [CHANGELOG.md](CHANGELOG.md) alone: the release notes
   are drafted from these messages when the maintainer releases.

## Testing against real HYSPLIT

runtraj includes no HYSPLIT, and NOAA ARL's builds are not ours to download in
CI, so CI tests runtraj against real HYSPLIT runs recorded where a build is
licensed. A recorded run (a *case*) is one call of a HYSPLIT program: the files
runtraj gave it (`CONTROL`, `SETUP.CFG`, ...), the files it wrote, what it
printed and its exit status. The cases are in `tests/recorded/hysplit/<version>/`,
with a `recorded.yaml` naming the build's SHA-256, its printed version, the
date and the met files. No HYSPLIT program and no met file is committed:
HYSPLIT's results from archived met may be shared, its programs may not.

We record the oldest HYSPLIT runtraj supports and the newest:

| Version | Build | Programs |
|---|---|---|
| 5.1.0 | the oldest supported (it writes no `age = 0` row) | `hycs_std` |
| 5.4.2 | NOAA ARL's public build, from [ARL's download page](https://www.ready.noaa.gov/HYSPLIT_linux.php) | `hycs_std`, `hyts_std` |

A version in between is recorded only when it changes something runtraj reads.

**Replaying.** `just test-replay VERSION` runs the integration tests with a
HYSPLIT folder whose programs replay the cases: each finds the case with the
same inputs, writes its files and exits as it did. A met folder in `CONTROL`
is compared as `<met>/`, so cases replay on any machine with the test met
files. CI runs it for each version, on Linux, macOS and Windows. `just replay-folder VERSION DEST` makes
such a folder for anything else; put the `exec/` it prints first on `PATH`.
The Kubernetes test replays its own cases, `tests/recorded/kubernetes/5.1.0/`
(`tests/kubernetes/README.md`). The docs build replays 5.1.0 the same way,
with the runs of its pages recorded beside the tests'.

**When a replay fails.** Inputs no case has fail the run, and the test session
lists them at its end ("HYSPLIT runs with no recording"), kept in
`tests/recorded/.misses/` to compare with the cases' `inputs/`. If runtraj now
writes them on purpose (a new setting, a new test), record again. A change that
should not change what runtraj writes is a bug the replay caught.

**Recording.** Recording needs each build, so it is done by hand, on a
machine where HYSPLIT is installed. List the builds in `hysplit-builds.toml`
at the repository's top (it is not committed), one HYSPLIT folder (`exec/`
and `bdyfiles/`) per version:

```toml
[builds]
"5.1.0" = "/path/to/hysplit-5.1.0"
"5.4.2" = "/path/to/hysplit.v5.4.2_RHEL8.10_public"
```

Record on the met files CI uses: the ones `RUNTRAJ_TEST_FETCH_MET=1` downloads
and crops into `tests/met_cache/` with the arlmet in `uv.lock` (23.9 GB
downloaded, 211 MB kept). Another copy of the same hours, cropped another way,
gives other numbers, and a replay stops at its start when the met files differ
from the ones in `recorded.yaml`. Then, with `RUNTRAJ_TEST_MET_DIR` set (or the
met files in `tests/met_cache/`):

```bash
just test-real        # the integration tests on each real build
just record           # replace tests/recorded/hysplit/<version>/ for each build
just record 5.4.2     # one version only
just record-kubernetes  # the Kubernetes test's runs, on 5.1.0, in tests/recorded/kubernetes/
```

`just record` runs the integration tests on the build with a folder whose
programs run the real one and save each call (for 5.1.0, the docs build too),
then writes `recorded.yaml`. A
case leaves out the files runtraj never reads (`NOT_RECORDED` in
`tests/fixtures/replay.py`, `PARTICLE.DAT` the largest), and the rest are
compressed with xz. Commit the cases with the
change that made them. HYSPLIT's random numbers make the particles differ from
run to run unless a run sets a seed, so a new recording changes every case's
output: record only when the inputs changed or a version is added.

## Writing documentation

The sources are in `docs/`: `getting_started/` (install, quickstart),
`guides/` (task-oriented how-tos, one task per page), `reference/` (API pages
built from docstrings, and the table of every `config.yaml` key, made by the
`config-model` directive in `docs/_ext/`), and `development/`
(architecture). A user-facing change updates the docs: the guide
when what the user does changes, the reference otherwise. `just build-docs`
must pass, and warnings are errors. The Documentation workflow builds the
docs on every pull request and publishes `main` as `dev/` and each release
under its version.

### Voice

Docs, docstrings, config field descriptions, and CLI help are written for
scientists who want to run the model and trust the result. Our models are the
xarray and MetPy user guides, and Herbie's tutorials.

- **Start from the reader's goal.** Open a page or section with what the
  reader wants to do and why it matters to their result. Then show it.
- **Recommend.** When there is a choice, say which one we would use and why
  ("Start with HRRR, because its 3 km grid resolves the valleys around a
  city"). "We" is the developers, "you" is the reader.
- **Give the reason in the same sentence.** Join cause and effect with
  "because" or "so". Keep one point per paragraph, and let a sentence be as
  long as its reason needs.
- **Show real output, and say what to notice in it.** Prefer an `ipython`
  block to a `code-block`, and a figure to a paragraph that describes one.
- **A guide holds what the reader acts on.** Exact rules, failure behavior,
  file-name rules and internals go in the docstring (Notes) and the
  reference. Measured accuracy goes on a Development page, with its test or
  study. A bug fix changes a guide only when it changes what the user does.
- **Say it once.** A caveat lives on one page, and the others link to it.
- **Use the reader's words.** Release, particles, meteorology.
  Internal names (store key, publish, resolve) belong only on pages about
  internals. Describe a config's fields (its parameters) in plain English,
  naming no class or module. `AGENTS.md` has the table of words to use.
- **Say it plainly.** No metaphors ("a dial worth turning"), no selling
  ("powerful", "seamless"), no filler ("note that", "it's worth noting").
  Use American spelling (meters, modeled, center).

Each kind of page has its own job:

- **Getting Started** tells one story, from a release to its particles,
  and says what to notice in each output.
- **User Guide** pages each cover one task, in the order a user meets them.
  A page opens with why the reader would do it, recommends a default, shows
  real output, and ends by linking to checking for when it goes wrong.
- **API Reference** pages are complete and exact, so the edge cases live
  there.
- **Development** pages hold the architecture.

Some habits make text read as machine-written. Avoid them:

- Colon reveals: "runtraj does one thing: it follows the air." Write
  "runtraj follows the air."
- "X, not Y" contrasts when nobody suggested Y.
- A bold sentence at the start of a paragraph that states its point.
- A page that begins "This page covers", and closing sentences that restate
  the paragraph.
- A run of sentences that each say what something does *not* do.
- Lists of three out of habit.
- Asides about what the code does "deliberately" or used to do.
- Labels invented on one line and referred to later ("the first case").

Before you commit a page, read it top to bottom as someone who has never run
a transport model. If a paragraph is there because of a bug fix, move it to the
reference.

Docstrings follow the NumPy style. The summary line says what the object is
or what the function returns. Parameter descriptions give the meaning and
units, not the type again. Check every example and claim against the code
when you write it.

### API pages

Each class has a page with tables of its attributes and methods, and each
member a page of its own; a subclass lists what it defines and links to what
it inherits. `docs/_templates/autosummary/class.rst` is runtraj's own (it
renders config models through `config-model`). `docs/_ext/api_pages.py`,
from python-template, decides which members get a row (not pydantic's, so no
list of them is kept) and what is inherited.

A property or attribute's page is where its See Also and Examples go, as in
pandas. Give each public one a docstring that says what it is, and add See
Also or Examples where they help; one with no docstring shows an empty row.
Napoleon reads a first line of the form `text: more text` of a property, an
attribute, or module data as `type: description`, so keep a colon out of
the middle of that line.

Module data such as `PARTICLE_SCHEMA` is listed under the package that
exports it (`runtraj.particles`). autodoc looks for its `#:` comment in that
module, where there is none, so `reexported_data_comment` in
`docs/conf.py` takes the comment from the submodule that defines it.
Without the hook, a page shows the docstring of the object's type
("Built-in immutable sequence.").

### Examples in the docs

Examples run when the docs build, so they show real output, and the build
fails when one breaks.

- **A figure in a docstring:** put a `.. plot::` directive in the Examples
  section. Its code runs (doctest `>>>` lines work), and the figure appears
  under it.

  ```rst
  Examples
  --------
  .. plot::

     >>> import matplotlib.pyplot as plt
     >>> _ = plt.plot([0, 1, 2], [0, 1, 4])
  ```

- **Code in an .rst page:** an `.. ipython:: python` block runs and shows
  each line with its output, as in an IPython session. Changing a
  `.. code-block:: python` to `.. ipython:: python` is enough to make it
  run. For a figure, put `@savefig name.png` on the line above the plotting
  call.

  ```rst
  .. ipython:: python

     import matplotlib.pyplot as plt
     import runtraj

     runtraj.__version__

     @savefig squares.png width=5in
     plt.plot([0, 1, 2], [0, 1, 4]);
  ```

  A block that raises or warns fails the build; `:okexcept:` or
  `:okwarning:` under the directive allows one that is meant to. All .rst
  pages share one session, so each page imports and defines what it uses.
  Split a long block so that no multi-line statement is its 10th (or 100th)
  statement: the directive runs only the first line of that one.
- **Examples in docstrings are tests too:** every `>>>` line under `src/` runs
  with `just test` (`--doctest-modules`), so an example that stops matching
  the code fails. One that needs a HYSPLIT run, met files or the network gets
  `# doctest: +SKIP` on its `>>>` line.
- **When they run:** `just build-docs` runs everything, as CI does.
  `just docs-serve` reruns a page's blocks whenever that page is rebuilt.
- **Notebooks, and code cells in Markdown pages,** are off in this project.
  `copier update --data docs_notebooks=true` turns them on; it adds myst-nb,
  and Jupyter with it, to the dev tools.
- **Data:** the docs build on GitHub Actions, so an example uses synthetic
  data, a small file in the repository, or a public download, never a
  machine-specific path. Seed random numbers so the figures don't change from
  build to build.
- **The Getting Started page runs a real simulation** on the met files the
  tests cache (`tests/met_cache`, 14 and 15 January 2021 around Salt Lake),
  in a temporary folder. `just build-docs` needs them: set
  `RUNTRAJ_TEST_MET_DIR` to that folder, or fetch them once with

  ```bash
  RUNTRAJ_TEST_FETCH_MET=1 uv run pytest \
    tests/models/hysplit/test_release_assignment.py::test_hysplit_multipoint_release_points_follow_control_order \
    --setup-only -q
  ```

  The fetch is large. NOAA's HRRR files cover the whole CONUS grid and are
  3.42 GB each; each is downloaded whole and then cropped to the 130 by 129
  cells around Salt Lake, which keeps 30.2 MB. The seven blocks the tests need
  therefore download 23.9 GB, all seven at once into `tests/met_cache`, so that
  folder's disk needs about 24 GB free while it runs. After the crop it holds
  211 MB. The time depends on your connection (at 10 MB/s it is about 40
  minutes). Fetch once, and keep the folder: a file already in it is not
  downloaded again.

  A release on the page must stay inside that time window. `docs.yml`
  restores the same cache the tests use.
- **The guides that open a project start from a sample project**
  (`docs/_ext/docs_examples.py`): four releases an hour apart on 15 January
  2021, run once per docs build (about 40 s) and copied into a temporary
  folder for each page. A page starts it from a hidden block
  (`docs_examples.temp_workdir(sample=True)`) and goes back with
  `os.chdir(docs_cwd)` at its end, so the page's files land in the
  temporary folder.
- **Optional dependencies:** the `dev` group installs
  `runtraj[complete]`, so the plotting extra (`visualization`) is there in
  CI. If an example needs another extra, add `runtraj[extra]` the same way.

## Adding configuration fields

`config.yaml` is blocks: `mets`, `models`, `stages`, and `variants` each map a
name to a block, and `ProjectConfig(...)` and `Project.init(...)` take the same
blocks. Inside a block the settings are flat: fields such as `seed`, `numpar`,
and `ziscale` are keys of the `hysplit` block under `models`. Don't add
nested parameter objects inside a block.

Each HYSPLIT setting is a field of `HysplitConfig`
(`src/runtraj/models/hysplit/config.py`): a plain pydantic
`Field(default, description=...)`. Which input file it goes to is the
HYSPLIT driver's business (`src/runtraj/models/hysplit/driver.py`). A
setting is a `SETUP.CFG` entry unless the driver lists it in
`NOT_IN_SETUP`: the `CONTROL` settings (which `ControlFile` writes),
`ziscale` (`ZICONTROL`), the wind- and mixed-layer-error groups (`WINDERR`
and `ZIERR`, in the order of `WIND_ERROR_SETTINGS` and
`ZI_ERROR_SETTINGS`), and the settings runtraj uses itself. So a new
`SETUP.CFG` entry needs only the field, and its HYSPLIT type in the table in
`tests/test_config.py`. Any other new setting also needs a line in the
driver; a test fails if it would land in `SETUP.CFG` unknown to HYSPLIT.

## Adding a transport model

A transport model is three classes: its config, its met config and the
model. Each subclasses a runtraj base class that has a default for all but
one or two members, so you write only what your model does differently.
This one leaves every particle where it was released:

```python
import numpy as np
import pandas as pd

from runtraj.models import MetConfig, ModelRun, TransportConfig, TransportModel


class StillConfig(TransportConfig):  # n_hours and seed come with the base
    n_particles: int = 10


class StillMet(MetConfig):  # one entry under mets:
    def settings(self):
        return {}  # no weather changes these particles


class StillModel(TransportModel[StillConfig]):
    name = "still"
    config_class = StillConfig
    met_config_class = StillMet
    needs_workdir = False  # it writes no files

    def run(self, release, config, met, window, workdir=None, timeout=None):
        hours = np.arange(abs(config.n_hours) + 1) * np.sign(config.n_hours)
        particles = pd.DataFrame(
            {
                "particle": np.tile(np.arange(1, config.n_particles + 1), len(hours)),
                "age": np.repeat(hours * 60, config.n_particles),  # minutes
                "lon": release.longitude,
                "lat": release.latitude,
                "zagl": release.altitude,
            }
        )
        return ModelRun(result=particles)
```

```yaml
models:
  still: {class: mypkg.models.StillModel, n_hours: -24}
```

A model in a package of its own needs no registration: its block under
`models:` names it by import path, and every process that opens the
project imports it from there. The run settings record the `class:`
string as written, so the same class named by two paths
(`mypkg.models.StillModel` and a re-export, `mypkg.StillModel`) gets two
settings folders. Pick one path and keep it. A model built into runtraj
goes in `runtraj.models.MODELS` under a short name instead. Keep its
package out of the core: the import contracts in `pyproject.toml` say how.

**The config** subclasses `runtraj.models.TransportConfig`, with the
model's own parameters.

- The base gives the parameters runtraj's own code reads whatever the
  model (`n_hours`, `seed`). A parameter goes there only when the core
  reads it, so another model is never handed a setting it would ignore.
- **`UNRECORDED`** is the set of fields that change no particle, empty
  unless the subclass names some.
- **`settings()`** is what a run records, and **`realizations(n)`** gives
  realization `k` with `seed + k`. Override them only where the model
  differs.

**The met config** subclasses `runtraj.models.MetConfig`, a pydantic model
that checks one entry under `mets:` and owns what is done with its files.
HYSPLIT's is `runtraj.models.hysplit.HysplitMetConfig`. A met that names no
files keeps every default but `settings()`.

- **`settings()`** is what a run records of the met: which weather it is,
  and anything else that changes the particles, never where its files are
  kept. HYSPLIT's are the source id in the ARL headers and the crop.
- **`check(releases, n_hours)`** returns what `runtraj check` lists before
  any run.
- **`fetch(windows)`** gets what `runtraj fetch` gets, and
  **`downloads()`** says whether `runtraj submit` gets the files once
  before the runs.
- **`open(files, bounds)`** returns the files as `runtraj.sampling` reads
  them: ARL file paths by default, or one Dataset in arlmet's layout.

**The model** subclasses `runtraj.models.TransportModel`, typed by its
config (`TransportModel[StillConfig]`). It sets `name`, `config_class` and
`met_config_class` and implements `run`. Every other member has a default
for a model that runs one release at a time, reads no files and makes
particles. runtraj calls each member directly, so a misspelled one
(`release_row` for `release_rows`) is never called; `check_model` reports
it.

- **`run(release, config, met, window, workdir=None, timeout=None)`**
  runs one release. `met` is the met config, with a field named
  `directory` or `subgrid_dir` made absolute from the project directory,
  and `window` the `(start, end)` the run covers; the model finds its own
  met from them. It returns a `ModelRun`: the result, the log as text, and
  the met files it read. A failed run raises `SimulationError` with its
  log. runtraj adds the release heights itself
  (`runtraj.models.run_model`).
- **The particle table** has the columns of
  `runtraj.particles.PARTICLE_SCHEMA`, `foot` if the model computes one,
  what a stage reads, and a release row at `age = 0` when the model can
  write one (`docs/reference/particles.rst`).
- **`release_rows(particles, release)`** is for a model that writes no
  row at `age = 0`: where each particle of a column or multipoint release
  started. Without either, such a release fails.
- **`result(config)`** is for a model that makes another type than
  particles: the type's name in `runtraj.results.RESULT_TYPES`
  (`concentration`, a `DataTree` of grids,
  `docs/reference/concentration.rst`; `trajectories`, the trajectory
  table, `docs/reference/trajectories.rst`). `check_model` holds the
  result to that type.
- **`check_releases(releases, config)`** lists the releases the model
  cannot run, as HYSPLIT does for a multipoint release whose points share
  a location. `runtraj check` shows them, and `run_model` and
  `run_model_many` fail them as `RELEASE_REFUSED` before `run` or
  `run_many` is called, so a batched model never has to refuse one release
  of a call.
- **`sha256(config)`**, **`version(config)`** and
  **`data_files(config)`** describe the build: the checksum of the
  executable it runs (`None` by default; part of the run's settings), what
  the build reports (recorded beside them), and the checksums of the data
  files a run reads. `sha256` raises `FileNotFoundError` when the
  executable cannot be found; the project still opens, and its status
  compares stored folders without the build.
- **`needs_workdir = False`** is for a model that writes no files; it is
  handed `workdir=None`.
- **`batched = True`** is for a model that runs many releases in one
  call, such as an emulator on a GPU. It gives `run_many(releases, config,
  met, windows, workdir=None, timeout=None)`, returning one table with a
  `release` column. The worker then hands it every release of a variant
  that needs particles at once, and a release it returns no rows for fails
  alone.

The toy transport model in `tests/fixtures/models.py` stands for a model in
another package, and is made unlike HYSPLIT: it runs no program, needs no
workdir, reads no met files, writes no log and no row at `age = 0`, and
numbers its particles 10, 20, 30, .... `tests/test_toy_model.py` runs the
core on it (run, status, check, fetch, a stage, transforms), so a change
that makes the core depend on something only HYSPLIT does fails there.
Test a change to the core on the toy as well as on HYSPLIT.

`runtraj.models.check_model(model, config, met, release)` checks the
promises a model keeps: its config uses no key runtraj records beside it
(`RESERVED_SETTINGS`) and its record reads back, also for its realizations;
its build makes a `ModelInfo`; and, given a release, the release is run
through the same code a project's run uses (`run` with a `timeout`, and
`run_many` with no workdir for a batched model), so its particles have the
table's columns, ages that go the way `n_hours` does, release heights, and
a particle file that stores them. A release its `check_releases` lists must
be refused, not run. Call it in the tests of a model of your own, with a
point, a column and a multipoint release.

A check in the Code Quality workflow (`.github/workflows/quality.yml`) keeps
HYSPLIT's setting names (`numpar`, `outdt`, `kmixd`, `varsiwant`, ...; the
list is in the workflow) in `runtraj/models/hysplit/`. Elsewhere a line may name one only
where its text also says HYSPLIT, in capitals, as a citation ("HYSPLIT's
`kmixd: 3`"); code that needs the setting belongs in HYSPLIT's package.

## Adding a stage

A stage is what is made from each simulation's results after the transport
model runs: one file per release, from the particles, from the files of
other stages, or both. PYSTILT's footprint is one
(`stilt.footprint.FootprintStage`). Write a stage when you want a product
made for every release of a project, kept in the output directory beside
the particles, and counted by `runtraj status`. For a calculation you run now
and then, read the particles (`project.particles`) instead.

A stage subclasses `runtraj.stages.Stage`, typed by its config, and holds
no state. It sets `name`, `config_class` and `suffix` and implements `run`;
the other members have defaults in the base class. This one writes each
release's mean particle height:

```python
from pydantic import BaseModel

from runtraj.stages import Stage


class MeanHeightConfig(BaseModel):
    below: float = 5000.0  # meters above ground


class MeanHeight(Stage[MeanHeightConfig]):
    name = "mean_height"  # its tree in the output, and its status() column
    config_class = MeanHeightConfig
    suffix = ".txt"  # the extension of its files

    def run(self, config, sim, path):
        zagl = sim.particles["zagl"]
        path.write_text(str(zagl[zagl < config.below].mean()))
```

```yaml
stages:
  mean_height:
    class: mypkg.stages.MeanHeight
    below: 3000
```

- **`name`** is the name of the stage's block under `stages:`, its tree in
  the output directory
  (`mean_height/settings=<variant>-<hash>/date=<day>/<release><suffix>`),
  its column in `project.status()`, and the `stage` of a failure record: one
  word for the stage everywhere, so a block under another name is refused.
  Lowercase letters, digits and underscores.
- **`suffix`** is the extension of the stage's files, with its dot.
- **`config_class`** is a pydantic model that checks the block's settings
  when `config.yaml` loads, without reading a file. A variant changes a
  setting under its own `stages:` key, and the variant's keys replace the
  block's; give the class an `override(block, own)` classmethod to merge
  them another way, as PYSTILT's `FootprintConfig` changes a grid field by field.
- **`resolve`** (by default, the config as it is) runs once for each
  variant when the project reads its variants. Read the files the settings name there, and raise `ValueError`
  for a transport model whose particles the stage cannot use (`model` is
  its name and `model_settings` what a run of it records). Return the
  config the runs use.
- **`settings`** (by default, every field: `config.model_dump(mode="json")`)
  returns the record that names the stage's settings folder, as plain data. A changed value is a new folder, so leave out what changes
  no result, such as a path. `config_class` must read the record back to a
  config with the same record, because a stored folder is found that way.
  `runtraj.stages.check_stage(stage, config)` checks it: call it in your
  tests, once for each kind of setting.
- **`check`** (by default, nothing) returns what would stop the stage for
  the project's releases, for `runtraj check`, as tuples of a short name,
  the release id or `None`, and a message. Run nothing in it.
- **`run`** makes one simulation's file and writes it at `path`. It gets the
  simulation: `sim.particles`, `sim.release`, `sim.variant`, and
  `sim.directory`, where a relative file name in the settings starts. Do
  not change `sim.particles` in place, since variants share it. Write one
  file whatever there is to say: a simulation is complete when the model's
  output file exists, and so does the file of every stage its variant uses,
  so a stage that writes nothing leaves its simulations unfinished. Write a
  file that says "nothing here", as an empty footprint does. An error
  fails the simulation at this stage, and the worker records it. On a
  filesystem `path` is a hidden temporary name ending in `suffix`, moved
  into place when `run` returns; the stage's earlier file is removed before
  it runs, and so is what a `run` that raises has written. So no file made
  from other inputs, and no half-written one, counts as complete. A Parquet
  file may carry `runtraj.identity.file_stamp(...)` in its metadata, the
  folder's hash and the runtraj version, as particle and footprint files do.
- **`reads`** (optional) names the stages whose files this one reads, such
  as `reads = ("footprint",)`. The stage runs after them and again whenever
  one was made again, and its settings folder is named from theirs too.
  Find a file with `sim.path("footprint")` and read it with the reader of
  the package that wrote it. A variant that sets a
  stage to null while another of its stages reads it is refused.
- **`result`** (optional) is the type of the transport model's result the
  stage reads (`runtraj.results`), `particles` when it does not say. A
  stage with `result = "concentration"` reads `sim.concentration`, and a
  variant whose model makes another type refuses it.
- **`version`** (optional, `None` by default) is the version of what the
  stage makes, such as `version = "2"`. When set it is part of the hash
  that names the stage's settings folders, and recorded as `stage_version`
  in their `_settings.yaml`. Raise it only for a fix that changes the
  stage's files: their old files then stop counting, and the stage makes
  new ones in a folder of its own. A stage that never sets it keeps the
  folders it has.

runtraj calls each member directly, so a misspelled one (`read` for
`reads`) is never called; `check_stage` reports it.

`path` is a `pathlib.Path`, or a `upath.UPath` when the output directory
is on an object store. Write through `path.open()`, which both have, and
the stage works with either. A table, for example:

```python
import pyarrow.parquet as pq

with path.open("wb") as f:
    pq.write_table(table, f, compression="zstd")
```

A stage imports only runtraj's public names, those in a module's
`__all__`, since only those keep their meaning from one version to the
next. A helper of runtraj's that a stage wants, it copies. The toy stages in
`tests/fixtures/stages.py` stand for a stage in another package and are held
to this by a test.

The stage's package must be installed wherever the project is opened, the
login node that reports status included: a settings folder is named from
the stage's settings, defaults included, and only its config class knows
them. A project whose config names a class that is not installed stops
with a message naming the package.

## Output directory and completion

A project is a local directory of inputs (`runtraj.Project`):
`config.yaml` and `releases.csv`. Results go to an output directory
(`runtraj.output.Output`) that `config.yaml` names and that several projects
can share. Each results folder is named by the hash of its settings, so a
changed setting writes a new folder and never overwrites a result.

`Output` knows where each result's file is (`path`), which releases have
one (`present`, from a listing of their date folders), and the one
definition of a finished simulation (`complete`, which applies
`runtraj.output.completed`): the model's output file exists, and so does the
file of every stage the variant uses. `Simulation.is_complete` and
`status()` use it. Every method takes the kind of result, `"particles"` or
a stage's name, so a stage needs no method of its own; it writes its file
at the path `make_path` gives. Don't add another "does this output exist"
check, a registry, or a manifest. Call these instead.

## Changing how work is run

`runtraj.execution.run` (`execution/runner.py`) finds the releases with
missing results. A local run calls `run_releases` in this process. A Slurm
run goes through `submit`, which hands the releases to the Slurm backend
(`execution/slurm.py`, beside `execution/kubernetes.py`); it writes `releases.txt`,
`releases.parquet` (the submitted releases' checked rows, which tasks
read in place of `releases.csv`), `execution.yaml`, and `job.sh`
(`job_script`) into `_slurm/<stamp>/` and calls `sbatch`. When a met
downloads, `fetch.sh` (`fetch_script`) is submitted first and the array
waits for it.

The unit of work is a command line: `runtraj run <project> --releases FILE
--task I/N`. Any scheduler that can start it with an index can run a
project; the task requeues itself on `SIGUSR1` inside a Slurm job. Another
scheduler means another script writer, not another worker.

A new `execution:` setting goes on its backend's block (`LocalConfig` or
`SlurmConfig` in `execution/config.py`) with a description; only a setting
that applies wherever a run happens goes on `ExecutionConfig` itself. A new
backend gets a block of its own, named as `backend:` names it. Settings that
only `sbatch` understands do not need a field: users write them in the
`slurm:` block by `sbatch`'s own names.

## Building distributions

runtraj is pure Python and includes no HYSPLIT, so one wheel and the source
archive serve every platform. Build and check them with:

```bash
just dist         # the sdist and the wheel, in dist/
```

## Releasing

The version comes from git tags, through setuptools-scm, so there is no
version string to bump.

1. Write the changelog: `just changelog` drafts the entries from the commit
   messages since the last release. Edit them into `CHANGELOG.md` under
   `## [Unreleased]`, then rename that heading to `## [X.Y.Z] - YYYY-MM-DD` and
   start a new empty `## [Unreleased]` above it. Commit (`chore(release): X.Y.Z`)
   and push to `main`.
2. Run `just release X.Y.Z`. It checks that the tree is clean, that `main` is in
   sync with GitHub, and that the version is newer than every existing tag, then
   pushes the tag `vX.Y.Z`.
3. The Publish workflow builds the tag with `just dist`, uploads it to PyPI and
   creates a GitHub Release from the CHANGELOG section. Zenodo archives the
   release and mints a DOI. The Documentation workflow publishes the docs as
   `X.Y.Z/` in the version dropdown.

Pre-releases (`0.1.0a23`, `0.1.0rc1`) work the same way. Until the first final
release, the newest pre-release is also the docs' default (`stable/`). Once a
final release is out, the pre-releases before it leave the dropdown (see
`DOCS_PRERELEASES` in `.github/workflows/docs.yml` to change that).

## Dependency updates

Dependabot opens one pull request a month for each of GitHub Actions and
pre-commit hooks. Merge it when CI passes.

`uv.lock` (the dev tools and every dependency) is updated by the Upgrade lock
workflow alone, not by Dependabot, which would leave a package at its old
version when the newest release needs a newer Python than the oldest one this
project supports. On the 1st of each month the workflow runs `uv lock --upgrade`
(`uv.lock` then holds the newest release for each Python; it never raises the
minimum versions in `pyproject.toml`) and, when the lock changes, opens the pull
request `chore/lock-upgrade` (or updates it). GitHub does not start workflows on
a pull request that its own token opened, so the workflow starts Tests, Code
Quality and Documentation on the branch itself; their results show on the pull
request. Merge it when they pass. You can also run it by hand: Actions > Upgrade
lock > Run workflow.

## Template

The tooling (CI workflows, pre-commit, justfile, packaging configuration) comes
from [jmineau/python-template](https://github.com/jmineau/python-template).
`.copier-answers.yml` records the template version; `copier update` pulls in
later template changes. Improvements that would help every package are best
made in the template.

## Pull requests

- Fix or add one thing per pull request, and link the issue it closes
  (`Fixes #N`).
- Leave `CHANGELOG.md` alone; the maintainer writes it at release, from your commit messages.
- Make sure the tests pass and coverage does not drop.

## Reporting bugs

Please include:

- your operating system and Python version
- steps to reproduce the problem
- what you expected and what happened
- any error messages or logs

## Feature requests and questions

Check the [existing issues](https://github.com/jmineau/runtraj/issues) first.
If nothing matches, open a new one describing the feature and why you need
it, or use the "question" label for questions.

## Code of conduct

Be respectful and constructive.

## License

Contributions are released under the project's MIT License.

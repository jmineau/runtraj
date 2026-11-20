<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/jmineau/runtraj/main/docs/_static/logo_dark.svg">
    <img alt="runtraj" src="https://raw.githubusercontent.com/jmineau/runtraj/main/docs/_static/logo.svg" height="64">
  </picture>
</h1>

[![Tests](https://github.com/jmineau/runtraj/actions/workflows/tests.yml/badge.svg)](https://github.com/jmineau/runtraj/actions/workflows/tests.yml)
[![Documentation](https://github.com/jmineau/runtraj/actions/workflows/docs.yml/badge.svg)](https://github.com/jmineau/runtraj/actions/workflows/docs.yml)
[![Code Quality](https://github.com/jmineau/runtraj/actions/workflows/quality.yml/badge.svg)](https://github.com/jmineau/runtraj/actions/workflows/quality.yml)
[![codecov](https://codecov.io/gh/jmineau/runtraj/branch/main/graph/badge.svg)](https://codecov.io/gh/jmineau/runtraj)
[![PyPI version](https://img.shields.io/pypi/v/runtraj.svg)](https://pypi.org/project/runtraj/)
[![Python Version](https://img.shields.io/pypi/pyversions/runtraj.svg)](https://pypi.org/project/runtraj/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.24384235.svg)](https://doi.org/10.5281/zenodo.24384235)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Pyrefly](https://img.shields.io/badge/types-pyrefly-blue.svg)](https://pyrefly.org)

runtraj runs particle dispersion models at scale. You describe where and when particles are
released and the settings to run them under, and it runs the transport model once for each,
on your machine or as a Slurm job array, and keeps the particles where you can find them again.
[HYSPLIT](https://www.ready.noaa.gov/HYSPLIT.php) is the model built in; another model, and
what is made from the particles afterwards, plug in by the import path of a class.

runtraj is not a model: it runs one, for every release and every set of settings, and
keeps track of what is done.

The guides and the API reference are at <https://jmineau.github.io/runtraj>.

## Status

runtraj is alpha software. There are no backward compatibility guarantees before v1.0, and the
public API may still change.

## Installation

Install it from PyPI:

```bash
pip install runtraj
```

For plotting and for downloading meteorology from NOAA, add the `complete` extra
(`runtraj[complete]`).

runtraj runs NOAA's HYSPLIT program, `hycs_std`, but does not include it or its data tables.
Download HYSPLIT from [NOAA's Air Resources Laboratory](https://www.ready.noaa.gov/HYSPLIT.php),
which asks you to accept the
[HYSPLIT terms of use](https://www.ready.noaa.gov/HYSPLIT_agreement.php) first. Unpack it, then
set `directory` under `models: hysplit:` in `config.yaml` to the folder it makes, or put that
folder's `exec/` on `PATH`:

```yaml
models:
  hysplit:
    directory: /path/to/hysplit
```

## Quickstart

A release is the time and place a run starts from. Run the transport model for one:

```python
import runtraj

release = runtraj.PointRelease(
    time="2023-07-15 18:00",   # UTC
    longitude=-111.848,
    latitude=40.766,
    altitude=10,               # meters above ground
)
met = "/data/hrrr"             # a folder of ARL files; their headers say what they hold

particles = runtraj.run_trajectories(release, met, n_hours=-24, numpar=100)
```

`particles` is a pandas DataFrame, one row per particle per step. A negative `n_hours` follows
the particles backward in time, a positive one forward.

For many releases, a project runs each one and keeps the results:

```python
project = runtraj.Project.init(
    "./my_project",
    releases=[release],
    mets={"hrrr": met},
    models={"hysplit": {"n_hours": -24, "numpar": 100}},
    variants={"hrrr": {}},
)
project.run()   # returns when the run is done

sim = project.simulation(release.id, "hrrr")
sim.particles
```

Everything is saved in `./my_project`. Later, open it again with
`runtraj.Project("./my_project")`. A second `run()` only runs what is missing. To change a
setting, edit `config.yaml`.

## Command line

```bash
runtraj check ./my_project         # list what would stop a run, before any run
runtraj run ./my_project           # run every release that is not done yet
runtraj run ./my_project --cpus 8  # the same, eight releases at a time
runtraj submit ./my_project        # the same, as a Slurm job array
runtraj status ./my_project        # what has finished, and the settings folders
```

`runtraj run` exits with 0 when every simulation it ran is complete, 1 when some failed, and 3
when it was stopped, or a connection error stopped some, before they finished.

## Variants

A variant is a named set of settings. Every release runs once under each variant, and a
variant's results are in a folder named for its settings, so a changed setting never
overwrites an earlier run:

```yaml
mets:
  hrrr: /data/hrrr
models:
  hysplit: {n_hours: -24, numpar: 100}
variants:
  hrrr: {}
  hrrr-long: {n_hours: -72}
```

## Plug-ins

A transport model in another package runs in place of HYSPLIT, and a stage makes one file per
release from the particles after the model runs. `config.yaml` names each by the import path
of its class, so runtraj imports neither until a project asks for it:

```yaml
stages:
  footprint:
    class: stilt.footprint.FootprintStage   # PYSTILT's footprint
```

## Citing

If you use runtraj in published work, please cite it through its
[Zenodo DOI](https://doi.org/10.5281/zenodo.24384235), which always points to the latest release.
GitHub's "Cite this repository" gives the reference from `CITATION.cff`.

## Use of AI coding agents

This project is developed with the help of AI coding agents, directed and reviewed by the
maintainer, who owns the design and the science.

## License

MIT. See [LICENSE](LICENSE).

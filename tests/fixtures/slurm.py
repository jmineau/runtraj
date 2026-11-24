"""Fake Slurm commands for the tests of the runner and the Slurm backend."""

from pathlib import Path
from types import SimpleNamespace

from runtraj.project import Project

from .factories import make_project_config


def out(stdout: str = "", returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    """Return what a fake ``subprocess.run`` of a Slurm command gives back."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def project_at(directory: Path, output: Path) -> Project:
    """Return a project in *directory* with an empty ``config.yaml``: the job script reads no setting."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.yaml").write_text("")
    return Project(directory, output=output)


def sbatch_options(script: str) -> dict[str, str | bool]:
    """Return the #SBATCH options of a script, a bare flag as True."""
    options: dict[str, str | bool] = {}
    for line in script.splitlines():
        if line.startswith("#SBATCH --"):
            key, _, value = line[len("#SBATCH --") :].partition("=")
            options[key] = value if value else True
    return options


def hourly_project(tmp_path, n: int) -> Project:
    """A project of *n* hourly releases, one met, and HYSPLIT, in ``hourly/``."""
    import datetime as dt

    from runtraj.releases import PointRelease

    config = make_project_config(tmp_path)
    releases = [
        PointRelease(
            time=dt.datetime(2023, 1, 1, h),
            longitude=-111.85,
            latitude=40.77,
            altitude=5.0,
        )
        for h in range(n)
    ]
    return Project.init(tmp_path / "hourly", config=config, releases=releases)

"""What a concentration run's grids must hold to be kept, checked on HYSPLIT's own files."""

import shutil
from pathlib import Path

from runtraj.models.hysplit import HysplitConfig
from runtraj.models.hysplit.control import cdump_name
from runtraj.models.hysplit.model import _read_grids

HERE = Path(__file__).parent / "cdump"


def _config(grid: dict, n_hours: int = 4, **fields) -> HysplitConfig:
    return HysplitConfig(
        n_hours=n_hours,
        mode="concentration",
        pollutants=[{"name": "TRCA"}, {"name": "TRCB"}],
        grids=[{"name": "g", **grid}],
        **fields,
    )


def _workdir(tmp_path: Path, fixture: str) -> Path:
    shutil.copy(HERE / fixture, tmp_path / cdump_name("g"))
    return tmp_path


def test_a_cmass_run_keeps_its_grids_labeled_as_mass(tmp_path):
    work = _workdir(tmp_path, "average_cpack1")
    grid = {"levels": [0, 100]}
    assert "m-3" in _read_grids(work, _config(grid))["g"]["TRCA"].attrs["units"]
    mass = _read_grids(work, _config(grid, cmass=1))["g"]["TRCA"]
    assert "m-3" not in mass.attrs["units"]
    assert "mass in the cell" in mass.attrs["long_name"]

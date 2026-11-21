"""What a concentration run's grids must hold to be kept, checked on HYSPLIT's own files."""

import shutil
from pathlib import Path

import pytest

from runtraj.exceptions import SimulationError
from runtraj.models.hysplit import HysplitConfig
from runtraj.models.hysplit.control import cdump_name
from runtraj.models.hysplit.failures import FailureReason
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


# The files are 4 hours forward from 01:30, hourly averages (4 samples) and
# 2-hourly snapshots (2 samples), as HYSPLIT writes them.
@pytest.mark.parametrize(
    ("fixture", "grid", "n_hours"),
    [
        ("average_cpack1", {"sampling_interval": 60}, 4),
        ("snapshot_cpack1", {"sampling": "snapshot", "sampling_interval": 120}, 4),
        # The part of a run after its last whole interval has no sample.
        ("snapshot_cpack1", {"sampling": "snapshot", "sampling_interval": 120}, 5),
        # A backward run has as many samples.
        ("snapshot_cpack1", {"sampling": "snapshot", "sampling_interval": 120}, -4),
    ],
)
def test_a_run_that_wrote_every_sample_is_kept(tmp_path, fixture, grid, n_hours):
    work = _workdir(tmp_path, fixture)
    tree = _read_grids(work, _config({"levels": [100], **grid}, n_hours))
    assert tree["g"].sizes["time"] > 0


@pytest.mark.parametrize(
    ("fixture", "grid", "n_hours", "wanted"),
    [
        ("average_cpack1", {"sampling_interval": 60}, 24, "4 of the 24"),
        ("average_cpack1", {"sampling_interval": 30}, 4, "4 of the 8"),
        ("average_cpack1", {"sampling_interval": 60}, -6, "4 of the 6"),
        (
            "snapshot_cpack1",
            {"sampling": "snapshot", "sampling_interval": 120},
            8,
            "2 of the 4",
        ),
    ],
)
def test_a_run_that_stopped_early_is_not_kept_as_complete(
    tmp_path, fixture, grid, n_hours, wanted
):
    work = _workdir(tmp_path, fixture)
    with pytest.raises(SimulationError, match=wanted) as caught:
        _read_grids(work, _config({"levels": [100], **grid}, n_hours))
    assert caught.value.reason == FailureReason.NO_CONCENTRATION_DATA
    assert "'g'" in str(caught.value)

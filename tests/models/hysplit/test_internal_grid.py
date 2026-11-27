"""Tests for the internal grid HYSPLIT prints in MESSAGE."""

import lzma
from pathlib import Path

import pytest

from runtraj.models.hysplit.driver import read_internal_grid

RECORDED = Path(__file__).parents[2] / "recorded" / "hysplit"


@pytest.mark.parametrize("version", ["5.0.2", "5.4.2"])
def test_the_grid_is_read_from_the_messages_of_recorded_runs(tmp_path, version):
    """Every recorded run, on the oldest HYSPLIT and a current one, prints one of the two grids of its top (25000 m or 10000 m)."""
    read = set()
    for xz in sorted((RECORDED / version / "hycs_std").glob("*/outputs/MESSAGE.xz")):
        (tmp_path / "MESSAGE").write_bytes(lzma.decompress(xz.read_bytes()))
        grid = read_internal_grid(tmp_path)
        if grid is not None:  # a case that never started HYSPLIT has none
            read.add(grid)

    assert read == {(41, 15.0, -5.0, 0.0), (33, 10.0, -10.0, 10.0)}


@pytest.mark.parametrize(
    "numbers",
    ["11 2.5 2.5 0.0", "11   2.50000000       2.50000000       0.00000000    "],
)
def test_the_line_is_read_in_the_form_of_every_version(tmp_path, numbers):
    (tmp_path / "MESSAGE").write_text(
        f" Internal grid parameters (nlvl,aa,bb,cc):  {numbers}\n"
    )

    assert read_internal_grid(tmp_path) == (11, 2.5, 2.5, 0.0)


def test_a_run_without_the_line_or_the_file_has_no_grid(tmp_path):
    assert read_internal_grid(tmp_path) is None
    (tmp_path / "MESSAGE").write_text(" Starting HYSPLIT\n")
    assert read_internal_grid(tmp_path) is None

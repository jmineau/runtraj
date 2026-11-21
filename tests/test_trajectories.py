"""The trajectory table: its check, and its file."""

import pandas as pd
import pyarrow.parquet as pq
import pytest

from runtraj.particles import stored_release
from runtraj.results import RESULT_TYPES
from runtraj.trajectories import (
    TRAJECTORY_SCHEMA,
    check_trajectories,
    read_trajectories,
    write_trajectories,
)


def _table(release) -> pd.DataFrame:
    start = pd.Timestamp(release.time, tz="UTC")
    return pd.DataFrame(
        {
            "trajectory": [1, 2, 1, 2],
            "time": [start, start, start - pd.Timedelta("1h")] * 1
            + [start - pd.Timedelta("1h")],
            "age": [0, 0, -60, -60],
            "lon": [-111.85, -111.85, -111.9, -111.8],
            "lat": [40.77, 40.77, 40.7, 40.8],
            "zagl": [5.0, 500.0, 12.5, 480.0],
            "pres": [860.0, 810.0, 859.0, 812.0],
        }
    )


def test_trajectories_is_a_result_type_stored_as_parquet():
    assert RESULT_TYPES["trajectories"].suffix == ".parquet"


def test_a_table_reads_back_as_it_was_written(tmp_path, point_release):
    table = _table(point_release)
    path = write_trajectories(
        tmp_path / "t.parquet",
        table,
        point_release,
        {"mode": "trajectory"},
        [tmp_path / "met.arl"],
        metadata={b"runtraj:hash": b"abc"},
    )
    back = read_trajectories(path)
    assert list(back.columns) == list(table.columns)
    assert back["age"].dtype == "int32"
    assert back["trajectory"].dtype == "int32"
    pd.testing.assert_frame_equal(
        back, table.astype({"age": "int32", "trajectory": "int32"}), check_dtype=False
    )
    # The file reads alone: its release, settings, and met files are in it.
    schema = pq.read_schema(path)
    assert "release" in schema.names
    assert stored_release(schema.metadata, path) == point_release
    assert schema.metadata[b"runtraj:settings"] == b'{"mode": "trajectory"}'
    assert schema.metadata[b"runtraj:hash"] == b"abc"


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(FileNotFoundError, match="No trajectory file"):
        read_trajectories(tmp_path / "none.parquet")


@pytest.mark.parametrize("column", list(TRAJECTORY_SCHEMA))
def test_a_table_needs_every_column_of_the_schema(point_release, column):
    with pytest.raises(ValueError, match=column):
        check_trajectories(_table(point_release).drop(columns=column))


def test_a_table_needs_a_row_and_to_be_a_table(point_release):
    with pytest.raises(ValueError, match="no trajectory"):
        check_trajectories(_table(point_release).iloc[:0])
    with pytest.raises(ValueError, match="DataFrame"):
        check_trajectories({"trajectory": [1]})

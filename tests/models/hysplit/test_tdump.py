"""HYSPLIT's trajectory file, read from files ``hyts_std`` wrote (``tdump/README.md``)."""

from pathlib import Path

import pandas as pd
import pytest

from runtraj.models.hysplit import read_tdump
from runtraj.trajectories import check_trajectories

HERE = Path(__file__).parent / "tdump"


def test_a_forward_file_has_a_row_per_trajectory_per_output_time():
    table = read_tdump(HERE / "forward")
    check_trajectories(table)
    assert table.attrs == {
        "trajectories": 3,
        "direction": "FORWARD",
        "vertical_motion": "OMEGA",
    }
    # 6 hours, a row every 30 minutes and one at the start, for each of 3.
    assert len(table) == 3 * 13
    assert sorted(table["trajectory"].unique()) == [1, 2, 3]
    assert list(table.columns) == [
        "trajectory",
        "time",
        "age",
        "lon",
        "lat",
        "zagl",
        "pres",
        "tpot",
        "temp",
        "rain",
        "mlht",
        "relh",
        "sphu",
        "mixr",
        "zsfc",
        "dswf",
    ]


def test_each_trajectory_starts_where_control_put_it():
    table = read_tdump(HERE / "forward")
    start = table[table["age"] == 0].set_index("trajectory")
    assert (start["time"] == pd.Timestamp("2021-07-15 02:00", tz="UTC")).all()
    assert start["lon"].tolist() == [-112.0, -112.0, -111.5]
    assert start["lat"].tolist() == [40.5, 40.5, 41.0]
    # Two trajectories from one place stay two: hyts_std joins no line source.
    assert start["zagl"].tolist() == [10.0, 500.0, 100.0]


def test_a_row_holds_the_values_of_its_line():
    # "2 1 21 7 15 4 30 1 2.5 41.019 -111.855 1090.2 682.5 319.4 286.4 0.0
    #  764.3 30.3 4.2 4.2 2305.2 0.0" in the file.
    table = read_tdump(HERE / "forward")
    row = table[(table["trajectory"] == 2) & (table["age"] == 150)].iloc[0]
    assert row["time"] == pd.Timestamp("2021-07-15 04:30", tz="UTC")
    assert row.drop(["trajectory", "time", "age"]).to_dict() == {
        "lon": -111.855,
        "lat": 41.019,
        "zagl": 1090.2,
        "pres": 682.5,
        "tpot": 319.4,
        "temp": 286.4,
        "rain": 0.0,
        "mlht": 764.3,
        "relh": 30.3,
        "sphu": 4.2,
        "mixr": 4.2,
        "zsfc": 2305.2,
        "dswf": 0.0,
    }


def test_age_is_in_minutes_from_each_rows_time_and_negative_backward():
    table = read_tdump(HERE / "backward")
    assert table.attrs["direction"] == "BACKWARD"
    # The release was at 10:30, a minute the file's header does not hold.
    assert table["time"].iloc[0] == pd.Timestamp("2021-07-15 10:30", tz="UTC")
    assert table["age"].tolist() == [0, -60, -120, -180, -240]
    assert list(table.columns)[6:] == ["pres"]


def test_a_value_that_fills_its_field_is_read_by_the_fields_width(tmp_path):
    # trjdsk.f writes 8I6,F8.1,2F9.3 and then 1X,F8.1 for each value.
    text = (HERE / "backward").read_text().splitlines()
    ints = "".join(f"{v:6d}" for v in (1, 1, 21, 7, 15, 9, 30, 1))
    text[7] = (
        f"{ints}{-1.0:8.1f}{-40.31:9.3f}{-111.851:9.3f} {123456.7:8.1f} {863.8:8.1f}"
    )
    path = tmp_path / "tdump"
    path.write_text("\n".join(text) + "\n")
    row = read_tdump(path).iloc[1]
    assert (row["lat"], row["lon"], row["zagl"]) == (-40.31, -111.851, 123456.7)
    assert row["pres"] == 863.8


def test_a_label_runtraj_does_not_know_keeps_hysplits_name(tmp_path):
    text = (HERE / "backward").read_text().replace("PRESSURE", "NEW_ONE ")
    path = tmp_path / "tdump"
    path.write_text(text)
    assert "new_one" in read_tdump(path).columns


def test_a_file_with_a_header_and_no_rows_is_an_empty_table(tmp_path):
    path = tmp_path / "tdump"
    path.write_text("\n".join((HERE / "backward").read_text().splitlines()[:6]) + "\n")
    table = read_tdump(path)
    assert table.empty
    assert table.attrs["trajectories"] == 1


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "not a HYSPLIT trajectory file"),
        ("     1     1\n    HRRR    21     7    15     0     1\n", "not a HYSPLIT"),
        ("     0     2\n     1 FORWARD  OMEGA\n", "format 2"),
    ],
)
def test_another_file_is_refused(tmp_path, text, match):
    path = tmp_path / "tdump"
    path.write_text(text)
    with pytest.raises(ValueError, match=match):
        read_tdump(path)

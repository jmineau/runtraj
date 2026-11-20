"""Tests for the particle table's ``.runtraj`` accessor: where particles first leave a box."""

import pandas as pd
import pytest

import runtraj

#: Two degrees by two: -112 to -110 east, 40 to 42 north.
BOX = runtraj.Bounds(xmin=-112, xmax=-110, ymin=40, ymax=42)


def _particles(*rows: tuple, time: bool = False) -> pd.DataFrame:
    """Return a table from ``(particle, age, lon, lat, zagl)`` rows."""
    p = pd.DataFrame(rows, columns=["particle", "age", "lon", "lat", "zagl"])
    if time:
        p["time"] = pd.Timestamp("2023-01-01 12:00") + pd.to_timedelta(
            p["age"], unit="min"
        )
    return p


def _exit(rows: list[tuple], box: runtraj.Bounds = BOX) -> pd.Series:
    """Return the one exit of a table made of one particle's rows."""
    exits = _particles(*rows).runtraj.grid_exits(box)
    assert len(exits) == 1
    return exits.iloc[0]


@pytest.mark.parametrize(
    ("edge", "last"),
    [
        ("n", (-111, 42.5)),
        ("e", (-109.5, 41)),
        ("s", (-111, 39.5)),
        ("w", (-112.5, 41)),
    ],
)
def test_the_edge_is_the_side_the_particle_left_by(edge, last):
    rows = [(1, 0, -111, 41, 10), (1, -60, last[0], last[1], 30)]
    assert _exit(rows)["edge"] == edge


def test_position_height_and_age_are_interpolated_onto_the_edge():
    """The step from -110.5 to -109.5 is crossed halfway, at -110."""
    exit_ = _exit(
        [
            (1, 0, -111, 41.0, 10),
            (1, -60, -110.5, 41.0, 20),
            (1, -120, -109.5, 42.0, 30),
        ]
    )
    assert exit_["edge"] == "e"
    assert exit_["lon"] == pytest.approx(-110.0)
    assert exit_["lat"] == pytest.approx(41.5)
    assert exit_["zagl"] == pytest.approx(25.0)
    assert exit_["age"] == pytest.approx(-90.0)


def test_time_is_interpolated_when_the_table_has_it():
    p = _particles(
        (1, 0, -111, 41, 10),
        (1, -60, -110.5, 41, 20),
        (1, -120, -109.5, 41, 30),
        time=True,
    )
    exits = p.runtraj.grid_exits(BOX)
    assert exits["time"].iloc[0] == pd.Timestamp("2023-01-01 10:30")
    assert "time" not in p.drop(columns="time").runtraj.grid_exits(BOX).columns


def test_a_particle_that_stays_inside_is_not_in_the_result():
    p = _particles(
        (1, 0, -111, 41, 10),
        (1, -60, -110.5, 41, 10),
        (2, 0, -111, 41, 10),
        (2, -60, -109, 41, 10),
    )
    exits = p.runtraj.grid_exits(BOX)
    assert exits["particle"].tolist() == [2]
    assert list(exits.columns) == ["particle", "edge", "lon", "lat", "zagl", "age"]


def test_the_first_exit_counts_when_a_particle_comes_back():
    """Out the north edge, then back in, then out the east: the exit is the north one."""
    exit_ = _exit(
        [
            (1, 0, -111, 41.5, 10),
            (1, -60, -111, 42.5, 10),
            (1, -120, -111, 41, 10),
            (1, -180, -109, 41, 10),
        ]
    )
    assert exit_["edge"] == "n"
    assert exit_["lat"] == pytest.approx(42.0)
    assert exit_["age"] == pytest.approx(-30.0)


def test_a_corner_exit_is_the_edge_crossed_first():
    """A step that crosses north and east: the exit is on the one it reaches first."""
    exit_ = _exit([(1, 0, -110.5, 41.5, 10), (1, -60, -109.5, 43.5, 10)])
    assert exit_["edge"] == "n"
    exit_ = _exit([(1, 0, -110.5, 41.5, 10), (1, -60, -108.5, 42.5, 10)])
    assert exit_["edge"] == "e"
    assert exit_["lon"] == pytest.approx(-110.0)
    assert exit_["lat"] == pytest.approx(41.75)


def test_rows_are_read_in_time_order_for_a_backward_and_a_forward_run():
    rows = [(1, 0, -111, 41, 10), (1, -60, -110.5, 41, 20), (1, -120, -109.5, 41, 30)]
    shuffled = _particles(*rows[::-1]).runtraj.grid_exits(BOX)
    assert shuffled["age"].tolist() == [-90.0]
    forward = [(1, 0, -111, 41, 10), (1, 60, -110.5, 41, 20), (1, 120, -109.5, 41, 30)]
    assert _particles(*forward[::-1]).runtraj.grid_exits(BOX)["age"].tolist() == [90.0]


def test_a_particle_whose_first_row_is_outside_is_reported_at_that_row():
    """No row inside to interpolate from: the row itself, on the edge it is farthest beyond."""
    exit_ = _exit([(1, -1, -109.0, 42.2, 10), (1, -61, -108, 42, 20)])
    assert exit_["edge"] == "e"
    assert (exit_["lon"], exit_["lat"], exit_["age"]) == (-109.0, 42.2, -1.0)


def test_a_point_on_the_edge_is_inside():
    p = _particles((1, 0, -111, 41, 10), (1, -60, -110.0, 42.0, 10))
    assert p.runtraj.grid_exits(BOX).empty


def test_a_box_across_the_dateline_is_read_modulo_360():
    for box in (
        runtraj.Bounds(xmin=170, xmax=190, ymin=-10, ymax=10),
        runtraj.Bounds(xmin=170, xmax=-170, ymin=-10, ymax=10),
    ):
        inside = _particles(
            (1, 0, 175, 0, 1), (1, -60, -175, 0, 1), (1, -120, 179, 5, 1)
        )
        assert inside.runtraj.grid_exits(box).empty
        east = _exit([(1, 0, -175, 0, 1), (1, -60, -169, 0, 1)], box)
        assert east["edge"] == "e"
        assert east["lon"] == pytest.approx(-170.0)
        west = _exit([(1, 0, 172, 0, 1), (1, -60, 168, 0, 1)], box)
        assert west["edge"] == "w"
        assert west["lon"] == pytest.approx(170.0)


def test_an_exit_across_180_has_its_longitude_wrapped():
    box = runtraj.Bounds(xmin=170, xmax=180.5, ymin=-10, ymax=10)
    exit_ = _exit([(1, 0, 179, 0, 1), (1, -60, -179, 0, 1)], box)
    assert exit_["edge"] == "e"
    assert exit_["lon"] == pytest.approx(-179.5)


def test_a_box_around_the_earth_has_only_a_north_and_a_south_edge():
    box = runtraj.Bounds(xmin=-180, xmax=180, ymin=-60, ymax=60)
    p = _particles(
        (1, 0, 179, 0, 1), (1, -60, -179, 0, 1), (2, 0, 0, 59, 1), (2, -60, 0, 61, 1)
    )
    exits = p.runtraj.grid_exits(box)
    assert exits["particle"].tolist() == [2]
    assert exits["edge"].tolist() == ["n"]


def test_an_empty_table_gives_an_empty_result_with_the_columns():
    exits = _particles().runtraj.grid_exits(BOX)
    assert exits.empty
    assert list(exits.columns) == ["particle", "edge", "lon", "lat", "zagl", "age"]


def test_endpoints_gives_one_row_per_particle_of_each_release():
    a = _particles((1, -10, -111.0, 41.0, 5), (1, -20, -111.5, 41.0, 5))
    b = _particles((1, -10, -111.0, 41.0, 5), (1, -20, -110.5, 41.0, 5))
    both = pd.concat([a.assign(release="A"), b.assign(release="B")], ignore_index=True)
    ends = both.runtraj.endpoints()
    assert ends["release"].tolist() == ["A", "B"]
    assert ends["lon"].tolist() == [-111.5, -110.5]


def test_grid_exits_gives_each_releases_own_exit():
    a = _particles((1, 0, -111.0, 41.0, 10), (1, -60, -109.0, 41.0, 10))
    b = _particles((1, 0, -111.0, 41.0, 10), (1, -60, -113.0, 41.0, 10))
    both = pd.concat([a.assign(release="A"), b.assign(release="B")], ignore_index=True)
    exits = both.runtraj.grid_exits(BOX)
    assert exits["release"].tolist() == ["A", "B"]
    assert exits["edge"].tolist() == ["e", "w"]

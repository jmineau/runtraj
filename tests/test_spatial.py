import numpy as np
import pandas as pd
import pytest

from runtraj import Bounds
from runtraj.spatial import is_longlat, same_crs

# ---------------------------------------------------------------------------
# Bounds.contains: longitudes count modulo 360
# ---------------------------------------------------------------------------


def _inside(xmin, xmax, longitudes, latitude=40.0):
    """Return the longitudes that a box from *xmin* to *xmax* and latitudes 30 to 50 holds."""
    box = Bounds(xmin=xmin, xmax=xmax, ymin=30, ymax=50)
    held = box.contains(longitudes, latitude)
    return [lon for lon, h in zip(longitudes, held, strict=True) if h]


@pytest.mark.parametrize(
    ("xmin", "xmax", "longitudes", "inside"),
    [
        # A box across the dateline or past 180.
        (-125, -100, [-111.9, -90, -130], [-111.9]),
        (170, -170, [175, -175, 0], [175, -175]),
        (170, 190, [175, -175, 185, 0], [175, -175, 185]),
        (-190, -170, [-175, 175, 185], [-175, 175, 185]),
        # A point on an edge is inside, written either way.
        (-125, -100, [-125, -100], [-125, -100]),
        (170, -170, [170, -170, 190, -190], [170, -170, 190, -190]),
        (170, 190, [170, 190, -170, -190], [170, 190, -170, -190]),
        (-190, -170, [-190, -170, 170, 190], [-190, -170, 170, 190]),
        # Just past an edge is outside.
        (170, -170, [169.99, -169.99], []),
        (-125, -100, [-125.01, -99.99], []),
        # A box a turn wide or more holds every longitude, whatever its edges.
        (-180, 180, [-180, 0, 180, 90], [-180, 0, 180, 90]),
        (0, 360, [-180, 0, 180, 359, -1], [-180, 0, 180, 359, -1]),
        (-200, 200, [-180, 0, 180, 195], [-180, 0, 180, 195]),
        (-10, 400, [-180, 0, 180, 195], [-180, 0, 180, 195]),
        # A box that reaches past 360 wraps.
        (350, 370, [355, -355, 5, -5, 0, 10, 11, 349], [355, -355, 5, -5, 0, 10]),
        # A box with no width holds its meridian only.
        (-111, -111, [-111, 249, -110.9], [-111, 249]),
        # West of east by more than half a turn: the box is the long way round.
        (-170, 170, [175, -175, 0, 170, -170], [0, 170, -170]),
    ],
)
def test_bounds_contains_counts_longitudes_modulo_360(xmin, xmax, longitudes, inside):
    assert _inside(xmin, xmax, longitudes) == inside


def test_bounds_contains_allows_rounding_at_an_edge_written_in_another_turn():
    box = Bounds(xmin=-175.3, xmax=-170.1, ymin=30, ymax=50)

    assert box.contains([184.7, -175.3, 189.9], 40.0).all()


def test_bounds_contains_checks_latitude_across_the_dateline():
    box = Bounds(xmin=170, xmax=-170, ymin=30, ymax=50)

    held = box.contains([175, -175, 175, -175], [40, 40, 60, 29.99])

    assert held.tolist() == [True, True, False, False]


def test_bounds_contains_takes_a_scalar_and_a_series():
    box = Bounds(xmin=-114, xmax=-109, ymin=37, ymax=42)

    assert bool(box.contains(-111.9, 40.8))
    assert not box.contains(pd.Series([-111.9, -90.0]), 40.8).all()
    assert box.contains(pd.Series([-111.9, -110.0]), 40.8).all()


def test_bounds_contains_does_not_hold_a_point_with_a_missing_coordinate():
    box = Bounds(xmin=-180, xmax=180, ymin=-90, ymax=90)

    assert box.contains([np.nan, 0.0], [0.0, np.nan]).tolist() == [False, False]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"xmin": np.nan}, "four finite numbers"),
        ({"ymax": np.inf}, "four finite numbers"),
        ({"ymin": 42, "ymax": 39}, "ymin < ymax"),
        ({"ymax": 95}, "ymin < ymax"),
    ],
)
def test_bounds_refuse_edges_no_point_can_be_inside(change, message):
    """A NaN or swapped crop box was accepted, and then held no point at all."""
    box = {"xmin": -114.0, "xmax": -110.0, "ymin": 39.0, "ymax": 42.0}
    with pytest.raises(ValueError, match=message):
        Bounds(**{**box, **change})


def test_bounds_refuse_west_and_east_swapped():
    with pytest.raises(ValueError, match="swapped"):
        Bounds(xmin=-109, xmax=-114, ymin=37, ymax=42)


def test_bounds_take_a_narrow_box_across_the_dateline_and_a_wide_one_past_180():
    assert Bounds(xmin=170, xmax=-170, ymin=-10, ymax=10).contains(180, 0)
    wide = Bounds(xmin=-109, xmax=246, ymin=37, ymax=42)
    assert wide.contains(116.4, 40.0) and not wide.contains(-111.9, 40.8)


# ---------------------------------------------------------------------------
# is_longlat and same_crs: the spellings of a geographic CRS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "crs",
    [
        "+proj=longlat +datum=WGS84",
        "+proj=latlong +datum=WGS84",
        "+proj=lonlat +datum=WGS84",
        "EPSG:4326",
        "WGS84",
    ],
)
def test_is_longlat_holds_each_spelling_of_a_geographic_crs(crs):
    assert is_longlat(crs)


@pytest.mark.parametrize(
    "crs",
    ["EPSG:32612", "+proj=utm +zone=12 +datum=WGS84", "+proj=lcc +lat_1=33 +lat_2=45"],
)
def test_is_longlat_refuses_a_projected_crs(crs):
    assert not is_longlat(crs)


def test_is_longlat_refuses_a_string_pyproj_cannot_read():
    assert not is_longlat("+proj=bogus")


def test_same_crs_matches_geographic_spellings_and_refuses_others():
    assert same_crs("+proj=latlong +datum=WGS84", "+proj=lonlat +datum=WGS84")
    assert same_crs("+proj=longlat +datum=WGS84", "EPSG:4326")
    assert same_crs("EPSG:32612", "EPSG:32612")
    assert not same_crs("EPSG:32612", "EPSG:32613")
    assert not same_crs("EPSG:32612", "+proj=latlong +datum=WGS84")

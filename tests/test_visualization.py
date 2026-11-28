"""Visualization smoke tests — all rendered against the non-interactive Agg backend."""

from __future__ import annotations

import datetime as dt

import matplotlib
import matplotlib.legend
import pandas as pd
import pytest

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # must come after use("Agg")
from matplotlib.layout_engine import ConstrainedLayoutEngine

from runtraj.releases import ColumnRelease, MultiPointRelease, PointRelease
from runtraj.spatial import Bounds
from runtraj.visualization import ProjectPlotAccessor, _draw_bounds_box, _make_ax

from .fixtures.factories import make_project_config

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def close_figures():
    """Close all matplotlib figures after each test to avoid resource leaks."""
    yield
    plt.close("all")


@pytest.fixture
def release():
    return PointRelease(
        time=dt.datetime(2023, 1, 1, 12),
        longitude=-111.85,
        latitude=40.77,
        altitude=5.0,
    )


@pytest.fixture
def col_release():
    return ColumnRelease(
        time=dt.datetime(2023, 1, 1, 12),
        longitude=-111.85,
        latitude=40.77,
        bottom=5.0,
        top=50.0,
    )


@pytest.fixture
def multi_release():
    return MultiPointRelease(
        time=dt.datetime(2023, 1, 1, 12),
        longitudes=[-111.85, -111.86, -111.84],
        latitudes=[40.77, 40.78, 40.76],
        altitudes=[5.0, 5.0, 5.0],
    )


@pytest.fixture
def box():
    return Bounds(xmin=-114.0, xmax=-111.0, ymin=39.0, ymax=42.0)


@pytest.fixture
def minimal_trajectories(release):
    data = pd.DataFrame(
        {
            "age": [-60.0, -30.0, 0.0],
            "lon": [-111.85, -111.90, -111.95],
            "lat": [40.77, 40.75, 40.73],
            "zagl": [100.0, 200.0, 300.0],
            "foot": [0.1, 0.2, 0.3],
        }
    )
    return data


# ---------------------------------------------------------------------------
# _make_ax
# ---------------------------------------------------------------------------


def test_make_ax_no_args():
    fig, ax = _make_ax()
    assert fig is not None
    assert ax is not None


def test_make_ax_with_extent():
    fig, ax = _make_ax(extent=(-115.0, -110.0, 38.0, 43.0))
    assert fig is not None
    assert ax is not None


def test_make_ax_draws_country_borders_not_state_lines():
    """US state lines suit one country, and cartopy downloads them on first use."""
    feature = pytest.importorskip("cartopy.feature")
    from cartopy.mpl.feature_artist import FeatureArtist

    _, ax = _make_ax()
    drawn = [a._feature for a in ax.get_children() if isinstance(a, FeatureArtist)]
    assert feature.BORDERS in drawn
    assert feature.STATES not in drawn


def test_make_ax_with_existing_ax():
    _, existing = plt.subplots()
    fig, ax = _make_ax(ax=existing)
    assert ax is existing


def test_make_ax_has_a_constrained_layout():
    """Each colorbar a map adds gets room, with its label."""
    fig, _ = _make_ax()
    assert isinstance(fig.get_layout_engine(), ConstrainedLayoutEngine)


def test_make_ax_leaves_the_layout_of_a_given_ax():
    fig, existing = plt.subplots()
    _make_ax(ax=existing)
    assert fig.get_layout_engine() is None


# ---------------------------------------------------------------------------
# _log10_safe
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _draw_bounds_box
# ---------------------------------------------------------------------------


def test_draw_bounds_box_adds_patch(box):
    _, ax = plt.subplots()
    _draw_bounds_box(ax, box, label="Domain")
    assert len(ax.patches) == 1


# ---------------------------------------------------------------------------
# ReleasePlotAccessor
# ---------------------------------------------------------------------------


def test_release_map_point_returns_axes(release):
    ax = release.plot.map()
    assert ax is not None


def test_release_map_column_returns_axes(col_release):
    ax = col_release.plot.map()
    assert ax is not None


def test_release_map_multipoint_returns_axes(multi_release):
    ax = multi_release.plot.map()
    assert ax is not None


def test_release_map_legend_has_a_fixed_place(release, monkeypatch):
    """
    The legend sits in a corner instead of searching for the emptiest spot.

    With loc="best", matplotlib tries every candidate against every point on the
    axes, and warns when that takes over a second, as it does with the particles
    of a simulation.
    """

    def search(self, *args, **kwargs):
        raise AssertionError("the legend searched for its best position")

    monkeypatch.setattr(matplotlib.legend.Legend, "_find_best_position", search)
    fig, ax = plt.subplots()
    release.plot.map(ax=ax)
    fig.canvas.draw()  # the legend is placed at draw time


def test_release_map_column_names_its_height_reference(col_release):
    """A column's heights are labeled with the reference they are given in."""
    for ref in ("agl", "msl"):
        ax = col_release.model_copy(update={"altitude_ref": ref}).plot.map()
        assert any(f"m {ref.upper()}" in t.get_text() for t in ax.texts)
        plt.close("all")


def test_release_map_multipoint_names_its_height_reference(multi_release):
    """The height colorbar names the reference the release's heights are in."""
    for ref in ("agl", "msl"):
        ax = multi_release.model_copy(update={"altitude_ref": ref}).plot.map()
        labels = [a.get_ylabel() for a in ax.figure.axes]
        assert f"Height {ref.upper()} (m)" in labels
        plt.close("all")


def test_release_map_with_domain(release, box):
    ax = release.plot.map(domain=box)
    assert ax is not None


def test_release_map_met_bounds(release, box):
    ax = release.plot.map(met_bounds=box)
    assert ax is not None


def test_release_map_reuses_ax(release):
    _, existing = plt.subplots()
    ax = release.plot.map(ax=existing)
    assert ax is existing


# ---------------------------------------------------------------------------
# ParticlesPlotAccessor
# ---------------------------------------------------------------------------


def test_trajectories_map_default(minimal_trajectories):
    ax = minimal_trajectories.runtraj.plot.map()
    assert ax is not None


def test_trajectories_map_color_by_zagl(minimal_trajectories):
    ax = minimal_trajectories.runtraj.plot.map(color_by="zagl")
    assert ax is not None


def test_trajectories_map_color_by_foot(minimal_trajectories):
    ax = minimal_trajectories.runtraj.plot.map(color_by="foot")
    assert ax is not None


def test_trajectories_map_colors_by_any_numeric_column(minimal_trajectories):
    particles = minimal_trajectories.assign(pres=[850.0, 860.0, 870.0])
    ax = particles.runtraj.plot.map(color_by="pres")
    assert ax.figure.axes[-1].get_ylabel() == "pres"


def test_trajectories_map_invalid_color_by_raises(minimal_trajectories):
    with pytest.raises(ValueError, match="no numeric column 'bad_col'.*'zagl'"):
        minimal_trajectories.runtraj.plot.map(color_by="bad_col")


def test_trajectories_map_refuses_a_text_column(minimal_trajectories):
    particles = minimal_trajectories.assign(site="WBB")
    with pytest.raises(ValueError, match="no numeric column 'site'"):
        particles.runtraj.plot.map(color_by="site")


def test_trajectories_map_reuses_ax(minimal_trajectories):
    _, existing = plt.subplots()
    ax = minimal_trajectories.runtraj.plot.map(ax=existing)
    assert ax is existing


# ---------------------------------------------------------------------------
# SimulationPlotAccessor
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ProjectPlotAccessor
# ---------------------------------------------------------------------------


def test_project_availability_empty():
    from unittest.mock import MagicMock

    project = MagicMock()
    project.releases = pd.DataFrame(columns=["location", "time"])

    ax = ProjectPlotAccessor(project).availability()
    assert ax is not None


def test_project_availability_with_sims(tmp_path, release):
    from runtraj import Project

    config = make_project_config(tmp_path)
    project = Project.init(tmp_path, config=config, releases=[release])
    assert len(project.simulations) == 1

    ax = project.plot.availability()
    assert ax is not None
    [bars] = ax.collections
    assert len(bars.get_paths()) == 1


def test_project_availability_bins_releases_by_day_and_location(release):
    from unittest.mock import MagicMock

    t = pd.Timestamp(release.time)
    project = MagicMock()
    project.releases = pd.DataFrame(
        {
            "location": ["a", "a", "a", "b"],
            "time": [t, t + pd.Timedelta("2h"), t + pd.Timedelta("1D"), t],
        }
    )

    ax = ProjectPlotAccessor(project).availability()

    a, b = ax.collections[:2]
    assert [len(a.get_paths()), len(b.get_paths())] == [2, 1]  # days with releases
    assert [label.get_text() for label in ax.get_yticklabels()] == ["a", "b"]


def test_project_availability_reuses_ax():
    from unittest.mock import MagicMock

    _, existing = plt.subplots()
    project = MagicMock()
    project.releases = pd.DataFrame(columns=["location", "time"])

    ax = ProjectPlotAccessor(project).availability(ax=existing)
    assert ax is existing


def test_project_availability_formats_the_figure_of_the_given_ax(release):
    """The dates are formatted on the ax's figure, not the current figure."""
    from unittest.mock import MagicMock

    fig, existing = plt.subplots()
    other = plt.figure()  # now the current figure
    default_bottom = other.subplotpars.bottom
    project = MagicMock()
    project.releases = pd.DataFrame(
        {"location": [release.location_id], "time": [release.time]}
    )

    ProjectPlotAccessor(project).availability(ax=existing)

    assert fig.subplotpars.bottom == pytest.approx(0.2)  # set by autofmt_xdate
    assert other.subplotpars.bottom == default_bottom


def test_project_availability_figure_keeps_long_location_labels(release):
    """A label of any length stays inside the figure, so a saved figure has it whole."""
    from unittest.mock import MagicMock

    project = MagicMock()
    project.releases = pd.DataFrame(
        {"location": ["-111.848_40.766_10-with-a-long-name"], "time": [release.time]}
    )

    ax = ProjectPlotAccessor(project).availability()

    fig = ax.figure
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    for text in [*ax.get_yticklabels(), ax.yaxis.label]:
        assert text.get_window_extent(renderer).x0 >= 0


def test_project_availability_rotates_the_dates_of_its_own_figure(release):
    from unittest.mock import MagicMock

    project = MagicMock()
    project.releases = pd.DataFrame(
        {"location": [release.location_id], "time": [release.time]}
    )

    ax = ProjectPlotAccessor(project).availability()

    assert {label.get_rotation() for label in ax.get_xticklabels()} == {30.0}


# ---------------------------------------------------------------------------
# Maps across 180 degrees
# ---------------------------------------------------------------------------


def test_central_longitude_is_zero_unless_the_extent_runs_past_180():
    from runtraj.visualization import _central_longitude

    assert _central_longitude(None) == 0.0
    assert _central_longitude((-114.0, -111.0, 39.0, 42.0)) == 0.0
    assert _central_longitude((-180.0, 180.0, -90.0, 90.0)) == 0.0
    assert _central_longitude((169.95, 190.05, 40.0, 50.0)) == pytest.approx(180.0)
    assert _central_longitude((-190.05, -169.95, 40.0, 50.0)) == pytest.approx(-180.0)


def test_plotting_without_matplotlib_names_the_extra(monkeypatch):
    """A missing matplotlib says to install the visualization extra."""
    import importlib
    import sys

    monkeypatch.setitem(sys.modules, "matplotlib", None)
    monkeypatch.delitem(sys.modules, "runtraj.visualization")
    with pytest.raises(ImportError, match=r"runtraj\[visualization\]"):
        importlib.import_module("runtraj.visualization")

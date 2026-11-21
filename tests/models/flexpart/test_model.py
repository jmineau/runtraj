"""Tests for FLEXPART's config, met, input files, and model, with a stand-in for its executable."""

import datetime as dt
import resource
import shutil
import stat
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

import runtraj
from runtraj.exceptions import MeteorologyError, SimulationError
from runtraj.models import get_model
from runtraj.models.flexpart import FlexpartConfig, FlexpartMetConfig, FlexpartModel
from runtraj.models.flexpart.config import flexpart_version
from runtraj.models.flexpart.inputs import available, command, partoptions, releases
from runtraj.models.flexpart.met import catalog, files_for
from runtraj.releases import ColumnRelease, MultiPointRelease, PointRelease

pytest.importorskip("eccodes")

from ...fixtures.grib import write_gfs

# Most runs here are on the GFS stand-in; the warning has its own test.
pytestmark = pytest.mark.filterwarnings("ignore:FLEXPART 11 does not mix:UserWarning")

SAMPLE = Path(__file__).parent / "data" / "partoutput_20230601180000_init.nc"
RELEASE = PointRelease(
    time="2023-06-01 18:00", longitude=-111.848, latitude=40.766, altitude=25
)
WINDOW = (dt.datetime(2023, 5, 31, 18), dt.datetime(2023, 6, 1, 18))

#: A stand-in for FLEXPART: copies the real sample into output/ and says it worked.
FAKE = """#!/bin/bash
test -f options/COMMAND && test -f options/RELEASES && test -f AVAILABLE || exit 2
ulimit -s > stack_limit
cp {sample} output/partoutput_20230601180000_init.nc
echo " CONGRATULATIONS: YOU HAVE SUCCESSFULLY COMPLETED A FLEXPART MODEL RUN!"
"""


@pytest.fixture
def checkout(tmp_path):
    """A FLEXPART checkout with a stand-in executable and the option files it reads."""
    root = tmp_path / "flexpart"
    (root / "src").mkdir(parents=True)
    (root / "options" / "SPECIES").mkdir(parents=True)
    for name in ("IGBP_int1.dat", "sfcdata.t", "sfcdepo.t"):
        (root / "options" / name).write_text("data\n")
    (root / "options" / "SPECIES" / "SPECIES_AIRTRACER").write_text(
        '&SPECIES_PARAMS\n PSPECIES="AIRTRACER",\n PWEIGHTMOLAR=29.0,\n /\n'
    )
    (root / "version").write_text("11.0-test\n")
    exe = root / "src" / "FLEXPART"
    exe.write_text(FAKE.format(sample=SAMPLE))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return root


@pytest.fixture
def gfs(tmp_path):
    """GRIB files every 12 h around the sample run's window."""
    directory = tmp_path / "gfs"
    directory.mkdir()
    for time in (
        "2023-05-31 06:00",
        "2023-05-31 18:00",
        "2023-06-01 06:00",
        "2023-06-01 18:00",
    ):
        write_gfs(directory / f"gfs_{time[:10]}_{time[11:13]}.grb2", time)
    return directory


def config(checkout, **kw):
    return FlexpartConfig(directory=checkout, **{"n_hours": -24, **kw})


# -- config --------------------------------------------------------------------


def test_config_refuses_a_seed(checkout):
    with pytest.raises(ValidationError, match="FLEXPART has no seed"):
        config(checkout, seed=1)


def test_config_needs_lsynctime_to_divide_loutstep(checkout):
    with pytest.raises(ValidationError, match="must divide loutstep"):
        config(checkout, loutstep=3600, lsynctime=700)


def test_config_refuses_cblflag_with_time_steps_flexpart_would_change(checkout):
    """FLEXPART raises CTL to 5 and IFINE until CTL * IFINE is 50, and caps LSYNCTIME at 1200 s."""
    for steps in (
        {},
        {"ctl": 5.0, "ifine": 4},
        {"ctl": 5.0, "ifine": 10, "lsynctime": 1800, "loutstep": 3600},
    ):
        with pytest.raises(ValidationError, match="cblflag: 1 needs ctl"):
            config(checkout, cblflag=1, **steps)
    assert config(checkout, cblflag=1, ctl=5.0, ifine=10).cblflag == 1


def test_config_records_threads_but_not_where_the_checkout_is(checkout):
    settings = config(checkout).settings()
    assert "directory" not in settings
    assert settings["threads"] == 1


def test_config_without_a_checkout_cannot_run():
    with pytest.raises(ValueError, match="Set directory"):
        _ = FlexpartConfig(n_hours=-24).executable


def test_version_from_a_version_file(checkout):
    assert flexpart_version(checkout) == "11.0-test"


def test_version_without_file_or_git(tmp_path):
    with pytest.raises(FileNotFoundError, match="no 'version' file"):
        flexpart_version(tmp_path)


def test_met_config_takes_a_bare_directory_and_records_the_grid(gfs):
    met = FlexpartMetConfig.model_validate(str(gfs))
    assert met.directory == gfs
    assert met.settings() == {"source": "gfs", "grid": "1x1 deg, 3x3"}


def test_met_config_reads_the_grid_past_a_file_that_is_not_grib(gfs):
    (gfs / "a_readme.txt").write_text("not GRIB\n")
    assert FlexpartMetConfig(directory=gfs).settings()["grid"] == "1x1 deg, 3x3"


def test_met_config_names_a_directory_without_grib_files(tmp_path):
    (tmp_path / "notes.txt").write_text("not GRIB\n")
    with pytest.raises(FileNotFoundError, match="No GRIB files match"):
        FlexpartMetConfig(directory=tmp_path).settings()


def test_get_model_finds_flexpart():
    assert isinstance(get_model("flexpart"), FlexpartModel)


# -- met files -----------------------------------------------------------------


def test_files_for_a_window_include_one_beyond_each_end(gfs):
    files = files_for(
        catalog(gfs), (dt.datetime(2023, 5, 31, 20), dt.datetime(2023, 6, 1, 5))
    )
    assert list(files) == [dt.datetime(2023, 5, 31, 18), dt.datetime(2023, 6, 1, 6)]


def test_catalog_leaves_out_the_index_cfgrib_writes_beside_a_file(gfs):
    """Cfgrib writes ``<file>.<hash>.idx`` beside a GRIB file, and ``pattern`` is ``*`` unless set."""
    (gfs / "gfs_2023-05-31_18.grb2.5b78f.idx").write_bytes(
        b"\x80\x04not a GRIB message"
    )
    assert len(catalog(gfs)) == 4


def test_available_refuses_a_file_name_with_a_space(tmp_path):
    """FLEXPART reads a name in AVAILABLE up to its first space."""
    with pytest.raises(ValueError, match="first space"):
        available({dt.datetime(2023, 6, 1): tmp_path / "gfs 0p50.grb2"})


def test_files_for_a_window_the_files_do_not_cover(gfs):
    with pytest.raises(MeteorologyError, match="do not cover"):
        files_for(catalog(gfs), (dt.datetime(2023, 6, 1, 12), dt.datetime(2023, 6, 2)))


def test_files_for_a_window_with_a_missing_file(gfs):
    (gfs / "gfs_2023-06-01_06.grb2").unlink()
    with pytest.raises(MeteorologyError, match="a file is missing"):
        files_for(catalog(gfs), WINDOW)


# -- input files ---------------------------------------------------------------


def test_command_runs_backward_over_the_window(checkout, gfs):
    text = command(config(checkout), WINDOW)
    for line in (
        "LDIRECT=-1,",
        "IBDATE=20230531,",
        "IBTIME=180000,",
        "IEDATE=20230601,",
        "IETIME=180000,",
        "IPOUT=1,",
        "IOUT=0,",
        "IOUTPUTFOREACHRELEASE=1,",
        "NXSHIFT=0,",
    ):
        assert f" {line}\n" in text
    assert text.startswith("&COMMAND\n") and text.endswith(" /\n")


def test_command_times_before_ten_are_integers(checkout, gfs):
    window = (dt.datetime(2023, 6, 1, 6), dt.datetime(2023, 6, 1, 9, 30))
    text = command(config(checkout, n_hours=3), window)
    assert " LDIRECT=1,\n" in text
    assert " IBTIME=60000,\n" in text and " IETIME=93000,\n" in text


def test_releases_of_a_point(checkout):
    text = releases(RELEASE, 1000)
    assert text.count("&RELEASE\n") == 1
    assert " PARTS=1000,\n" in text and " ZKIND=1,\n" in text
    assert " Z1=25.0,\n" in text and " Z2=25.0,\n" in text


def test_releases_of_a_column_stack_one_particle_per_slab():
    column = ColumnRelease(
        time="2023-06-01 18:00", longitude=-111.8, latitude=40.7, bottom=0, top=100
    )
    text = releases(column, 4)
    assert text.count("&RELEASE\n") == 4
    assert text.count(" PARTS=1,\n") == 4
    for z1, z2 in [(0.0, 25.0), (25.0, 50.0), (50.0, 75.0), (75.0, 100.0)]:
        assert f" Z1={z1},\n Z2={z2},\n" in text


def test_releases_of_a_multipoint_split_the_particles():
    release = MultiPointRelease(
        time="2023-06-01 18:00",
        longitudes=[-111.8, -111.9, -112.0],
        latitudes=[40.7, 40.8, 40.9],
        altitudes=[1000, 2000, 3000],
        altitude_ref="msl",
    )
    text = releases(release, 10)
    assert [line for line in text.splitlines() if "PARTS" in line] == [
        " PARTS=4,",
        " PARTS=3,",
        " PARTS=3,",
    ]
    assert text.count(" ZKIND=2,\n") == 3


def test_release_rows_of_a_column_put_particle_k_in_slab_k():
    column = ColumnRelease(
        time="2023-06-01 18:00", longitude=-111.8, latitude=40.7, bottom=0, top=100
    )
    particles = pd.DataFrame({"particle": [3, 1, 4, 2], "age": -60})

    rows = FlexpartModel().release_rows(particles, column)

    assert rows["particle"].tolist() == [1, 2, 3, 4]
    assert rows["height"].tolist() == [12.5, 37.5, 62.5, 87.5]


def test_release_rows_of_a_multipoint_follow_the_releases_split():
    release = MultiPointRelease(
        time="2023-06-01 18:00",
        longitudes=[-111.8, -111.9, -112.0],
        latitudes=[40.7, 40.8, 40.9],
        altitudes=[1000, 2000, 3000],
        altitude_ref="msl",
    )
    particles = pd.DataFrame({"particle": range(1, 11), "age": -60})

    rows = FlexpartModel().release_rows(particles, release)

    # releases() gives the points 4, 3 and 3 particles, released in order.
    assert rows["height"].tolist() == [1000] * 4 + [2000] * 3 + [3000] * 3
    assert rows["lon"].tolist() == [-111.8] * 4 + [-111.9] * 3 + [-112.0] * 3


def test_run_trajectories_of_a_column_gives_release_heights(checkout, gfs, tmp_path):
    column = ColumnRelease(
        time="2023-06-01 18:00", longitude=-111.848, latitude=40.766, bottom=0, top=50
    )
    particles = runtraj.run_trajectories(
        column,
        {"directory": str(gfs)},
        model="flexpart",
        directory=str(checkout),
        n_hours=-24,
        workdir=tmp_path / "work",
    )

    numpar = particles["particle"].max()
    first = particles.groupby("particle")["release_height"].first()
    assert first.tolist() == [(k - 0.5) * 50 / numpar for k in first.index]


def test_partoptions_turn_on_the_met_at_each_particle():
    text = partoptions()
    for name in ("LONGITUDE", "HEIGHT", "MIXINGHEIGHT", "DENSITY", "PRESSURE"):
        assert f" {name}=.true.,\n" in text
    assert " PV=.false.,\n" in text


# -- the model -----------------------------------------------------------------


def test_run_writes_the_inputs_and_reads_the_particles(checkout, gfs, tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    run = FlexpartModel().run(
        RELEASE, config(checkout), FlexpartMetConfig(directory=gfs), WINDOW, workdir
    )

    assert len(run.result) == 5 * 24
    assert "CONGRATULATIONS" in run.log
    assert [p.name for p in run.met_files] == [
        "gfs_2023-05-31_18.grb2",
        "gfs_2023-06-01_06.grb2",
        "gfs_2023-06-01_18.grb2",
    ]
    assert (workdir / "pathnames").read_text() == (
        "./options/\n./output/\n./met/\n./AVAILABLE\n"
    )
    assert sorted(p.name for p in (workdir / "met").iterdir()) == [
        p.name for p in run.met_files
    ]
    assert (workdir / "options" / "SPECIES" / "SPECIES_001").is_file()
    assert (workdir / "options" / "IGBP_int1.dat").is_symlink()


@pytest.mark.filterwarnings("default:FLEXPART 11 does not mix:UserWarning")
def test_run_on_gfs_warns_that_flexpart_does_not_mix(checkout, gfs, tmp_path):
    with pytest.warns(UserWarning, match="does not mix its particles on GFS"):
        FlexpartModel().run(
            RELEASE,
            config(checkout),
            FlexpartMetConfig(directory=gfs),
            WINDOW,
            tmp_path,
        )


def test_run_trajectories_returns_flexpart_s_particles(checkout, gfs, tmp_path):
    particles = runtraj.run_trajectories(
        RELEASE,
        {"directory": str(gfs)},
        model="flexpart",
        directory=str(checkout),
        n_hours=-24,
        workdir=tmp_path / "work",
    )

    assert len(particles) == 5 * 24
    # The met at each particle, for a stage to read.
    assert {"mlht", "dens", "pres", "zsfc"} <= set(particles.columns)


def test_sha256_is_the_executable_s(checkout):
    import hashlib

    exe = checkout / "src" / "FLEXPART"
    assert (
        FlexpartModel().sha256(config(checkout))
        == hashlib.sha256(exe.read_bytes()).hexdigest()
    )


def test_eta_runs_the_eta_build(checkout):
    assert config(checkout, eta=True).executable == checkout / "src" / "FLEXPART_ETA"
    with pytest.raises(FileNotFoundError, match="make eta=yes"):
        FlexpartModel().sha256(config(checkout, eta=True))


def test_era5_runs_on_the_meter_build(checkout, gfs, tmp_path):
    """FLEXPART's meter build reads ECMWF data too (seen on a real ERA5 run)."""
    met = FlexpartMetConfig(source="era5", directory=gfs)
    run = FlexpartModel().run(RELEASE, config(checkout), met, WINDOW, tmp_path)
    assert len(run.result)


def test_gfs_refuses_the_eta_build(checkout, gfs, tmp_path):
    met = FlexpartMetConfig(directory=gfs)
    with pytest.raises(ValueError, match="eta: false"):
        FlexpartModel().run(RELEASE, config(checkout, eta=True), met, WINDOW, tmp_path)


@pytest.mark.skipif(
    sys.platform == "darwin", reason="macOS refuses a stack above 64 MB"
)
def test_run_raises_the_stack_limit_to_the_hard_limit(checkout, gfs, tmp_path):
    """FLEXPART's documentation asks for ``ulimit -s unlimited`` with OpenMP."""
    soft, hard = resource.getrlimit(resource.RLIMIT_STACK)
    expected = "unlimited" if hard == resource.RLIM_INFINITY else str(hard // 1024)
    # A low soft limit here, as many systems set, which FLEXPART would inherit.
    resource.setrlimit(resource.RLIMIT_STACK, (8 * 1024 * 1024, hard))
    try:
        FlexpartModel().run(
            RELEASE,
            config(checkout),
            FlexpartMetConfig(directory=gfs),
            WINDOW,
            tmp_path,
        )
    finally:
        resource.setrlimit(resource.RLIMIT_STACK, (soft, hard))
    assert (tmp_path / "stack_limit").read_text().strip() == expected


def test_run_goes_on_where_the_stack_limit_cannot_be_raised(
    checkout, gfs, tmp_path, monkeypatch
):
    """MacOS reports no hard limit and refuses the raise; FLEXPART still runs."""

    def refuse(*args):
        raise ValueError("current limit exceeds maximum limit")

    monkeypatch.setattr(resource, "setrlimit", refuse)
    run = FlexpartModel().run(
        RELEASE, config(checkout), FlexpartMetConfig(directory=gfs), WINDOW, tmp_path
    )
    assert len(run.result)


def test_version_is_none_without_file_or_git(tmp_path):
    assert FlexpartModel().version(config(tmp_path)) is None


def test_run_fails_with_flexpart_s_output(checkout, gfs, tmp_path):
    exe = checkout / "src" / "FLEXPART"
    exe.write_text("#!/bin/bash\necho ' #### FLEXPART MODEL ERROR! bad'\nexit 1\n")
    workdir = tmp_path / "work"
    workdir.mkdir()

    with pytest.raises(SimulationError, match="MODEL ERROR") as caught:
        FlexpartModel().run(
            RELEASE,
            config(checkout),
            FlexpartMetConfig(directory=gfs),
            WINDOW,
            workdir,
        )
    assert caught.value.reason == "EXIT_STATUS"
    assert "MODEL ERROR" in caught.value.log


def test_run_without_particle_output(checkout, gfs, tmp_path):
    exe = checkout / "src" / "FLEXPART"
    exe.write_text("#!/bin/bash\necho ' CONGRATULATIONS'\n")
    workdir = tmp_path / "work"
    workdir.mkdir()

    with pytest.raises(SimulationError, match="no partoutput") as caught:
        FlexpartModel().run(
            RELEASE,
            config(checkout),
            FlexpartMetConfig(directory=gfs),
            WINDOW,
            workdir,
        )
    assert caught.value.reason == "NO_PARTICLE_DATA"


def test_run_timeout(checkout, gfs, tmp_path):
    exe = checkout / "src" / "FLEXPART"
    exe.write_text("#!/bin/bash\nsleep 30\n")
    workdir = tmp_path / "work"
    workdir.mkdir()

    with pytest.raises(SimulationError, match="timeout") as caught:
        FlexpartModel().run(
            RELEASE,
            config(checkout),
            FlexpartMetConfig(directory=gfs),
            WINDOW,
            workdir,
            timeout=1,
        )
    assert caught.value.reason == "TIMEOUT"


def test_run_refuses_missing_met_before_running(checkout, gfs, tmp_path):
    shutil.rmtree(checkout / "src")  # would fail if it ran
    window = (dt.datetime(2023, 6, 2), dt.datetime(2023, 6, 3))
    workdir = tmp_path / "work"
    workdir.mkdir()

    with pytest.raises(MeteorologyError):
        FlexpartModel().run(
            RELEASE,
            config(checkout),
            FlexpartMetConfig(directory=gfs),
            window,
            workdir,
        )
    assert not np.any([p.name == "options" for p in workdir.iterdir()])


def test_a_project_runs_flexpart(checkout, gfs, tmp_path):
    from runtraj import Project

    project = Project.init(
        tmp_path / "proj",
        [RELEASE],
        models={"flexpart": {"n_hours": -24, "directory": str(checkout)}},
        mets={"gfs": {"directory": str(gfs)}},
        variants={"gfs": {}},
        output=str(tmp_path / "out"),
    )

    project.run(compute_root=tmp_path / "compute")

    assert project.status()["state"].tolist() == ["complete"]
    # A new Project reads the result back from the output directory.
    particles = Project(tmp_path / "proj").particles()
    assert len(particles) == 5 * 24
    assert "mlht" in particles.columns

"""Tests for HysplitConfig: the HYSPLIT parameters and how they are checked."""

import pytest
from pydantic import ValidationError

from runtraj.models.hysplit import HysplitConfig

from ...fixtures.factories import make_project_config


def test_winderrtf_partial_xy_raises():
    with pytest.raises(ValidationError):
        HysplitConfig(n_hours=-24, siguverr=1.0)  # only one of four XY params set


def test_winderrtf_partial_zi_raises():
    with pytest.raises(ValidationError):
        HysplitConfig(n_hours=-24, sigzierr=0.6, tlzierr=60.0)  # missing horcorzierr


def test_hysplit_config_flat_construction():
    """HysplitConfig accepts all fields flat (no met fields)."""
    p = HysplitConfig(
        n_hours=-24,
        numpar=500,
    )
    assert p.n_hours == -24
    assert p.numpar == 500


def test_hysplit_config_ziscale_defaults_to_scalar_one():
    p = HysplitConfig(n_hours=-24)
    assert p.ziscale == 1.0


def test_saved_config_no_longer_carries_zicontroltf():
    assert "zicontroltf" not in HysplitConfig(n_hours=-24, ziscale=0.8).model_dump()


def test_zicontroltf_is_not_a_setting():
    with pytest.raises(ValidationError, match="zicontroltf"):
        HysplitConfig(n_hours=-24, zicontroltf=1)


@pytest.mark.parametrize("ziscale", [0.0, [1.0, 0.0]])
def test_ziscale_zero_is_rejected(ziscale):
    with pytest.raises(ValueError, match="ziscale of 0"):
        HysplitConfig(n_hours=-24, ziscale=ziscale)


def test_ziscale_empty_list_is_rejected():
    with pytest.raises(ValueError, match="cannot be empty"):
        HysplitConfig(n_hours=-24, ziscale=[])


def test_ziscale_rejects_multiple_per_simulation_lists():
    with pytest.raises(ValueError, match="Per-simulation ziscale lists"):
        HysplitConfig(n_hours=-24, ziscale=[[0.8, 0.8], [0.9, 0.9]])


@pytest.mark.parametrize("krand", [0, 1, 3, 4, 10, 13])
def test_seed_requires_krand_2(krand):
    with pytest.raises(ValueError, match="requires krand=2"):
        HysplitConfig(n_hours=-24, seed=1, krand=krand)


@pytest.mark.parametrize("seed", [-1, -2, -17])
def test_a_negative_seed_is_refused(seed):
    # -(abs(seed) + 1) gave a seed and its negative the same run, and the
    # realizations of seed=-2 (-2, -1, 0, 1) repeated one another.
    with pytest.raises(ValueError, match="must be 0 or more"):
        HysplitConfig(n_hours=-24, seed=seed, krand=2)


@pytest.mark.parametrize("seed", [0, 1, 17])
def test_a_seed_of_0_or_more_is_accepted(seed):
    assert HysplitConfig(n_hours=-24, seed=seed, krand=2).seed == seed


def test_realizations_of_an_accepted_seed_never_go_negative():
    # A seed of 0 or more plus k, for k from 0, stays 0 or more.
    config = HysplitConfig(n_hours=-24, seed=0, krand=2)
    assert [r.seed for r in config.realizations(5)] == [0, 1, 2, 3, 4]


def test_a_project_with_a_negative_seed_and_realizations_is_refused(tmp_path):
    # seed=-2 with 4 realizations ran seeds -2, -1, 0, 1, so two were the same run.
    with pytest.raises(ValueError, match="must be 0 or more"):
        make_project_config(
            tmp_path,
            n_hours=-1,
            krand=2,
            seed=-2,
            variants={"hrrr": {"realizations": 4}},
        )


@pytest.mark.parametrize("krand", [0, 1, 2, 3, 4, 10, 11, 12, 13])
def test_krand_accepts_hysplit_modes(krand):
    assert HysplitConfig(n_hours=-24, krand=krand).krand == krand


@pytest.mark.parametrize("krand", [-1, 5, 9, 14, 20])
def test_krand_rejects_undocumented_values(krand):
    with pytest.raises(ValueError, match="krand"):
        HysplitConfig(n_hours=-24, krand=krand)


def test_varsiwant_takes_zcfx_the_name_hysplit_reads():
    config = HysplitConfig(
        n_hours=-24, varsiwant=["time", "indx", "long", "lati", "zagl", "zcfx"]
    )
    assert config.varsiwant[-1] == "zcfx"


@pytest.mark.parametrize("name", ["zfx1", "ZFX1"])
def test_varsiwant_refuses_zfx1_and_names_zcfx(name):
    """hycs_std stops at lowercase `zfx1` and reads `ZFX1` only in capitals, which no other name takes."""
    with pytest.raises(ValidationError, match="'zfx1' is not a varsiwant name.*'zcfx'"):
        HysplitConfig(n_hours=-24, varsiwant=["time", "indx", "long", "lati", name])


@pytest.mark.parametrize("name", ["zfxx", "TIME", "height"])
def test_varsiwant_refuses_a_name_hysplit_does_not_read(name):
    with pytest.raises(ValidationError, match="varsiwant"):
        HysplitConfig(n_hours=-24, varsiwant=["time", "indx", "long", "lati", name])


@pytest.mark.parametrize("code", ["time", "indx", "long", "lati", "zagl"])
def test_varsiwant_refuses_a_particle_run_missing_a_column_of_the_table(code):
    """HYSPLIT writes only the columns varsiwant names, and the particle table needs these five."""
    names = [n for n in ("time", "indx", "long", "lati", "zagl", "mlht") if n != code]
    with pytest.raises(ValidationError, match=f"varsiwant lacks '{code}'"):
        HysplitConfig(n_hours=-6, varsiwant=names)


def test_varsiwant_names_every_missing_code():
    with pytest.raises(ValidationError, match="lacks 'indx', 'time', 'long', 'lati'"):
        HysplitConfig(n_hours=-6, varsiwant=["zagl"])


def test_varsiwant_is_not_checked_where_the_mode_ignores_it():
    """The concentration and trajectory modes do not read it, so a block set for particle variants may serve them."""
    short = ["time", "indx"]
    conc = HysplitConfig(
        n_hours=-6,
        mode="concentration",
        varsiwant=short,
        pollutants=[{"name": "TRCA"}],
        grids=[
            {"name": "g", "spacing_lat": 0.05, "spacing_lon": 0.05, "levels": [0, 100]}
        ],
    )
    assert conc.varsiwant == short
    assert (
        HysplitConfig(n_hours=-6, mode="trajectory", varsiwant=short).varsiwant == short
    )


@pytest.mark.parametrize("numpar", [0, -5])
def test_numpar_must_be_at_least_one(numpar):
    with pytest.raises(ValidationError, match="numpar"):
        HysplitConfig(n_hours=-24, numpar=numpar)


def test_n_hours_must_not_be_zero():
    with pytest.raises(ValidationError, match="n_hours must not be 0"):
        HysplitConfig(n_hours=0)
    assert HysplitConfig(n_hours=6).n_hours == 6


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("z_top", -100.0),
        ("z_top", 0.0),
        ("outdt", -1),
        ("emisshrs", -0.01),
        ("veght", -1.0),
        ("veght", 0.0),
        ("veght", float("nan")),
    ],
)
def test_a_physical_setting_out_of_its_range_is_refused(field, value):
    """These were accepted and ran, or failed later with no field named."""
    with pytest.raises(ValidationError, match=field):
        HysplitConfig(n_hours=-24, **{field: value})


@pytest.mark.parametrize(
    "field",
    ["siguverr", "tluverr", "zcoruverr", "horcoruverr"],
)
def test_a_negative_wind_error_setting_is_refused(field):
    errors = {
        "siguverr": 2.6,
        "tluverr": 260.0,
        "zcoruverr": 450.0,
        "horcoruverr": 14.0,
    }
    with pytest.raises(ValidationError, match=field):
        HysplitConfig(n_hours=-24, **{**errors, field: -errors[field]})


@pytest.mark.parametrize("field", ["sigzierr", "tlzierr", "horcorzierr"])
def test_a_negative_mixed_layer_error_setting_is_refused(field):
    errors = {"sigzierr": 0.6, "tlzierr": 60.0, "horcorzierr": 40.0}
    with pytest.raises(ValidationError, match=field):
        HysplitConfig(n_hours=-24, **{**errors, field: -errors[field]})


def test_a_negative_ziscale_is_kept_as_hysplits_switch_to_the_met_pbl_height():
    """HYSPLIT reads a negative factor as: use the met files' own PBL height."""
    assert HysplitConfig(n_hours=-24, ziscale=-1.0).ziscale == -1.0

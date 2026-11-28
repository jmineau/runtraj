"""Tests for runtraj.config - models and YAML roundtrip."""

import re
import textwrap

import pytest
import yaml
from pydantic import ValidationError

from runtraj.config import ProjectConfig, _UniqueKeyLoader
from runtraj.execution import ExecutionConfig
from runtraj.models.hysplit.driver import winderrtf
from tests.fixtures.factories import blocks

from .fixtures.factories import make_met_config, write_arl_file


@pytest.fixture(autouse=True)
def _met_header(tmp_path):
    """Put an ARL file in each test's met folder: resolving a project reads its met's source there."""
    write_arl_file(tmp_path / "met" / "header.arl")


# ---------------------------------------------------------------------------
# winderrtf
# ---------------------------------------------------------------------------
# ProjectConfig - the blocks
# ---------------------------------------------------------------------------


def test_a_models_wrong_value_is_reported_for_its_block(tmp_path):
    """A block is checked when the file loads, as the variants that change none of it run it."""
    mets = _met_config(tmp_path)
    with pytest.raises(
        ValueError, match=r"models: hysplit \(hysplit\): [\s\S]*n_hours must not be 0"
    ):
        ProjectConfig(
            mets=mets, models={"hysplit": {"n_hours": 0}}, variants={"hrrr": {}}
        )
    with pytest.raises(
        ValueError, match=r"Variant 'hrrr' \(hysplit\): [\s\S]*n_hours must not be 0"
    ):
        ProjectConfig(
            models={"hysplit": {"n_hours": -24}},
            mets=mets,
            variants={"hrrr": {"n_hours": 0}},
        ).resolve()


def test_a_models_block_name_is_plain_as_a_variants_is(tmp_path):
    """A model's block name is what a variant writes, so it follows the variants' rule."""
    with pytest.raises(
        ValueError,
        match="Model name 'Not_Plain' may hold only lowercase letters, digits and hyphens",
    ):
        ProjectConfig(
            mets=_met_config(tmp_path),
            variants={"hrrr": {}},
            models={"Not_Plain": {}},
        )


def test_a_met_no_variant_uses_is_still_checked(tmp_path):
    """With one model, that model's met config checks every met, used or not."""
    mets = {**_met_config(tmp_path), "spare": {"subgrid_enable": "maybe"}}
    with pytest.raises(
        ValueError, match=r"Met 'spare' \(hysplit\): [\s\S]*subgrid_enable"
    ):
        ProjectConfig(
            mets=mets, models={"hysplit": {"n_hours": -24}}, variants={"hrrr": {}}
        )


def test_a_config_or_a_block_that_is_not_a_mapping_is_an_error(tmp_path):
    """The checks of keys leave anything that is not a mapping to pydantic's own message."""
    from runtraj.execution.config import ExecutionConfig, SlurmConfig

    with pytest.raises(ValidationError, match="ProjectConfig"):
        ProjectConfig.model_validate(["mets", "variants"])
    with pytest.raises(ValidationError, match="mets"):
        ProjectConfig(mets=str(tmp_path / "met"), variants={"hrrr": {}})
    with pytest.raises(ValidationError, match="ExecutionConfig"):
        ExecutionConfig.model_validate("slurm")
    with pytest.raises(ValidationError, match="SlurmConfig"):
        SlurmConfig.model_validate("02:00:00")


def test_model_config_requires_nonempty_mets():
    """ProjectConfig must have at least one met entry."""
    with pytest.raises(Exception, match="at least one"):
        ProjectConfig(
            models={"hysplit": {"n_hours": -24}}, mets={}, variants={"hrrr": {}}
        )


def test_model_config_rejects_met_keys_that_cannot_name_a_variant(tmp_path):
    mc = make_met_config(directory=tmp_path / "met")
    with pytest.raises(Exception, match="Met names may hold only lowercase letters"):
        ProjectConfig(
            models={"hysplit": {"n_hours": -24}},
            mets={"hrrr_v2": mc},
            variants={"hrrr_v2": {}},
        )


def test_a_config_loads_and_resolves_without_its_hysplit_build(tmp_path):
    """The build is read on first use, so a machine without hycs_std opens the project."""
    mc = make_met_config(directory=tmp_path / "met")
    elsewhere = tmp_path / "not-mounted"
    cfg = ProjectConfig(
        **blocks(mets={"hrrr": mc}, directory=elsewhere, variants={"hrrr": {}})
    )
    variant = cfg.resolve()["hrrr"]
    with pytest.raises(FileNotFoundError, match="hycs_std"):
        _ = variant.build
    with pytest.raises(
        ValueError, match="realizations must be a whole number, 1 or more"
    ):
        ProjectConfig(
            models={"hysplit": {"n_hours": -24}},
            mets={"hrrr": mc},
            variants={"e": {"realizations": 0}},
        )


# ---------------------------------------------------------------------------
# ProjectConfig YAML roundtrip
# ---------------------------------------------------------------------------


def test_model_config_yaml_roundtrip_basic(tmp_path):
    cfg = ProjectConfig(
        mets={"hrrr": make_met_config(directory=tmp_path / "met")},
        models={"hysplit": {"n_hours": -24, "numpar": 100}},
        variants={"hrrr": {}},
    )
    path = tmp_path / "config.yaml"
    cfg.to_yaml(path)
    loaded = ProjectConfig.from_yaml(path)
    text = path.read_text()

    assert "#" not in text
    assert loaded.models == {"hysplit": {"n_hours": -24, "numpar": 100}}
    assert "hrrr" in loaded.mets
    assert list(yaml.safe_load(text)) == ["mets", "models", "variants"]


def test_model_config_reports_invalid_yaml(tmp_path):
    """Malformed YAML is reported as an invalid config file, not a parser traceback."""
    path = tmp_path / "config.yaml"
    path.write_text("n_hours: [\n")

    with pytest.raises(
        ValueError, match=r"^config\.yaml is not valid YAML at line 2, column 1: "
    ):
        ProjectConfig.from_yaml(path)


@pytest.mark.parametrize(
    ("text", "where"),
    [
        (
            "models:\n  hysplit:\n    n_hours: -24\n    numpar: 100\n    n_hours: 24\n",
            "line 5, column 5: 'n_hours'",
        ),
        (
            "variants:\n  a:\n    numpar: 10\n    numpar: 20\n",
            "line 4, column 5: 'numpar'",
        ),
    ],
    ids=["block", "variant"],
)
def test_model_config_refuses_a_repeated_key(tmp_path, text, where):
    """A key written twice is refused, not read as its last value."""
    path = tmp_path / "config.yaml"
    path.write_text(text)

    with pytest.raises(ValueError) as error:
        ProjectConfig.from_yaml(path)
    assert str(error.value) == (
        f"config.yaml is not valid YAML at {where} is written twice."
    )


@pytest.mark.parametrize("text", ["12:00:00", "2:00:00", "02:00:00", "'12:00:00'"])
@pytest.mark.parametrize("key", ["slurm.time", "pbs.walltime"])
def test_an_unquoted_clock_time_stays_text(text, key):
    """``time: 12:00:00`` is 12 hours, not the base-60 integer 43200 (30 days)."""
    block, setting = key.split(".")
    raw = yaml.load(
        f"backend: {block}\n{block}:\n  {setting}: {text}\n", Loader=_UniqueKeyLoader
    )
    value = getattr(getattr(ExecutionConfig.model_validate(raw), block), setting)
    assert value == text.strip("'")


def test_a_plain_number_is_still_a_number():
    assert yaml.load("a: 12\nb: 1.5\n", Loader=_UniqueKeyLoader) == {"a": 12, "b": 1.5}


def test_model_config_yaml_roundtrip_with_execution(tmp_path):
    cfg = ProjectConfig(
        models={"hysplit": {"n_hours": -24}},
        mets=_met_config(tmp_path),
        execution={
            "backend": "slurm",
            "slurm": {
                "n_workers": 8,
                "time": "02:00:00",
                "partition": "compute",
                "account": "lin-group",
                "exclude": "node1",
            },
        },
        variants={"hrrr": {}},
    )
    path = tmp_path / "config.yaml"
    text = cfg.to_yaml(path)
    assert "cpus" not in text and "array_parallelism" not in text  # unset stays out
    assert "local:" not in text
    loaded = ProjectConfig.from_yaml(path)
    assert loaded.execution == cfg.execution
    assert loaded.execution.backend == "slurm"
    assert loaded.execution.slurm.n_workers == 8
    assert loaded.execution.slurm.options == {"exclude": "node1"}


def test_execution_defaults_to_one_local_process(tmp_path):
    execution = ProjectConfig(
        models={"hysplit": {"n_hours": -24}},
        mets=_met_config(tmp_path),
        variants={"hrrr": {}},
    ).execution
    assert (execution.backend, execution.cpus) == ("local", 1)
    assert (execution.slurm.n_workers, execution.slurm.cpus) == (1, 1)
    assert (
        "execution:"
        not in ProjectConfig(
            models={"hysplit": {"n_hours": -24}},
            mets=_met_config(tmp_path),
            variants={"hrrr": {}},
        ).to_yaml()
    )


def test_each_backends_settings_are_in_its_own_block():
    """``cpus`` is the block's that ``backend`` names; the other block is not read."""
    from runtraj.execution.config import ExecutionConfig

    settings = {
        "local": {"cpus": 2},
        "slurm": {"cpus": 8, "n_workers": 40, "time": "04:00:00"},
        "timeout": 600,
    }
    local = ExecutionConfig.model_validate(settings)
    slurm = ExecutionConfig.model_validate({"backend": "slurm", **settings})
    assert (local.backend, local.cpus) == ("local", 2)
    assert (slurm.backend, slurm.cpus) == ("slurm", 8)
    assert local.timeout == slurm.timeout == 600
    # A config may hold a slurm block with no time and run locally.
    assert ExecutionConfig.model_validate({"slurm": {"n_workers": 4}}).cpus == 1
    with pytest.raises(ValueError, match="execution.slurm.time is required"):
        ExecutionConfig.model_validate({"backend": "slurm", "slurm": {"n_workers": 4}})


@pytest.mark.parametrize(
    ("setting", "blocks"),
    [
        (
            {"cpus": 4},
            "execution: local:, execution: slurm:, execution: pbs: or "
            "execution: kubernetes:, for the backend it is for.",
        ),
        ({"completions": 4}, "execution: kubernetes:."),
        (
            {"n_workers": 8},
            "execution: slurm: or execution: pbs:, for the backend it is for.",
        ),
        ({"time": "02:00:00"}, "execution: slurm:."),
        ({"partition": "compute"}, "execution: slurm:."),
        (
            {"fetch": False},
            "execution: local:, execution: slurm:, execution: pbs: or "
            "execution: kubernetes:, for the backend it is for.",
        ),
    ],
)
def test_a_backends_setting_beside_backend_is_refused_naming_its_block(
    tmp_path, setting, blocks
):
    (key,) = setting
    message = f"'{key}' is a setting of a backend. Write it under {blocks}"
    with pytest.raises(ValueError, match=re.escape(message)):
        ProjectConfig(
            models={"hysplit": {"n_hours": -24}},
            mets=_met_config(tmp_path),
            execution=setting,
            variants={"hrrr": {}},
        )


def test_unknown_execution_setting_is_an_error(tmp_path):
    """A typo, or an sbatch option outside `slurm:`, used to be ignored or passed on."""
    with pytest.raises(
        ValueError,
        match=r"'partion' is not a setting of execution\. Did you mean 'partition', under execution: slurm:\?",
    ):
        ProjectConfig(
            models={"hysplit": {"n_hours": -24}},
            mets=_met_config(tmp_path),
            execution={"partion": "compute"},
            variants={"hrrr": {}},
        )
    with pytest.raises(ValueError, match=r"'requeue' is not a setting of execution\."):
        ProjectConfig(
            models={"hysplit": {"n_hours": -24}},
            mets=_met_config(tmp_path),
            execution={"requeue": True},
            variants={"hrrr": {}},
        )
    with pytest.raises(
        ValueError, match=r"'cpus_per_task' is not a setting of execution\. \["
    ):
        ProjectConfig(
            models={"hysplit": {"n_hours": -24}},
            mets=_met_config(tmp_path),
            execution={"cpus_per_task": 4},
            variants={"hrrr": {}},
        )
    with pytest.raises(ValueError, match="backend"):
        ProjectConfig(
            models={"hysplit": {"n_hours": -24}},
            mets=_met_config(tmp_path),
            execution={"backend": "kubernetes"},
            variants={"hrrr": {}},
        )
    with pytest.raises(ValueError, match="n_workers"):
        ProjectConfig(
            models={"hysplit": {"n_hours": -24}},
            mets=_met_config(tmp_path),
            execution={"slurm": {"n_workers": 0}},
            variants={"hrrr": {}},
        )
    with pytest.raises(ValueError, match=r"execution\.local\.n_workers\s+Extra inputs"):
        ProjectConfig(
            models={"hysplit": {"n_hours": -24}},
            mets=_met_config(tmp_path),
            execution={"local": {"n_workers": 2}},
            variants={"hrrr": {}},
        )


def test_a_misspelled_slurm_setting_is_not_sent_to_sbatch(tmp_path):
    """Any other key of the slurm block is an sbatch option, so a near miss of a setting is refused."""
    from runtraj.execution.config import SlurmConfig

    for typo, setting in (("partion", "partition"), ("n_worker", "n_workers")):
        with pytest.raises(
            ValueError,
            match=f"'{typo}' is not a setting of execution.slurm. Did you mean '{setting}'",
        ):
            SlurmConfig.model_validate({typo: 1})
    options = {"exclude": "node1", "constraint": "skl", "cpus-per-task": 8, "ntasks": 1}
    assert SlurmConfig.model_validate(options).options == options


def test_execution_accepts_one_setup_line():
    from runtraj.execution.config import SlurmConfig

    assert SlurmConfig.model_validate({"setup": "module load hysplit"}).setup == [
        "module load hysplit"
    ]


def _met_config(tmp_path):
    return {
        "hrrr": make_met_config(
            directory=tmp_path / "met",
        )
    }


def test_model_config_unknown_keys_raise(tmp_path):
    """from_yaml must fail fast on unrecognized keys."""
    yaml_text = textwrap.dedent(f"""\
        mets:
          hrrr:
            directory: {tmp_path / "met"}
        mystery_param: 99
        variants:
          hrrr: {{}}
    """)
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text)

    with pytest.raises(ValidationError, match="mystery_param"):
        ProjectConfig.from_yaml(path)


# -- variants --------------------------------------------------------------------


def _variant_config(tmp_path, **kwargs):
    return ProjectConfig(
        **blocks(mets=_met_config(tmp_path), **{"variants": {"hrrr": {}}, **kwargs})
    )


def test_variants_default_to_one_per_met(tmp_path):
    mc = _met_config(tmp_path)["hrrr"]
    cfg = ProjectConfig(
        **blocks(
            mets={"hrrr": mc, "gfs": mc}, ziscale=0.9, variants={"hrrr": {}, "gfs": {}}
        )
    )
    variants = cfg.resolve()
    assert list(variants) == ["hrrr", "gfs"]
    assert variants["gfs"].met == "gfs"
    assert variants["gfs"].transport.ziscale == 0.9
    assert variants["hrrr"].transport.ziscale == 0.9


def test_variant_must_name_its_met_when_there_are_several(tmp_path):
    mc = _met_config(tmp_path)["hrrr"]
    with pytest.raises(ValueError, match="must name its met"):
        ProjectConfig(
            models={"hysplit": {"n_hours": -24}},
            mets={"hrrr": mc, "gfs": mc},
            variants={"a": {}},
        )
    with pytest.raises(ValueError, match="unknown met"):
        ProjectConfig(
            models={"hysplit": {"n_hours": -24}},
            mets={"hrrr": mc},
            variants={"a": {"met": "nam"}},
        )


@pytest.mark.parametrize("name", ["Hrrr", "hrrr_v2", "-x", "a b"])
def test_variant_names_are_plain(tmp_path, name):
    with pytest.raises(ValueError) as err:
        _variant_config(tmp_path, variants={name: {}})
    assert (
        f"Variant name {name!r} may hold only lowercase letters, digits and hyphens, "
        "and must start with a letter or digit"
    ) in str(err.value)
    assert "^[" not in str(err.value)


def test_an_ensemble_is_one_variant_whose_realizations_have_their_own_seed(tmp_path):
    cfg = _variant_config(
        tmp_path,
        krand=2,
        seed=42,
        variants={
            "hrrr": {},
            "err": {
                "siguverr": 1.0,
                "tluverr": 60.0,
                "zcoruverr": 500.0,
                "horcoruverr": 40.0,
                "realizations": 3,
            },
        },
    )
    variants = cfg.resolve()
    assert list(variants) == ["hrrr", "err"]
    err = variants["err"]
    assert err.realizations == 3 and err.realization_numbers == [0, 1, 2]
    assert err.transport.seed == 42  # the base seed
    assert [err.transport_for(k).seed for k in range(3)] == [42, 43, 44]
    assert winderrtf(err.transport_for(1)) == 1
    assert winderrtf(variants["hrrr"].transport) == 0
    assert variants["hrrr"].realization_numbers == [None]
    with pytest.raises(ValueError, match="realizations"):
        err.transport_for(3)
    err.check_realization(2)
    variants["hrrr"].check_realization(None)
    with pytest.raises(ValueError, match=r"runs as realizations \[0, 1, 2\], not None"):
        err.check_realization(None)
    with pytest.raises(ValueError, match=r"runs as realizations \[None\], not 0"):
        variants["hrrr"].check_realization(0)


@pytest.mark.parametrize("value", [2.5, 2.0, True, False, "3", [3], 0, -1])
def test_realizations_must_be_a_whole_number_of_one_or_more(tmp_path, value):
    with pytest.raises(
        ValueError,
        match=rf"Variant 'ens'.*realizations must be a whole number.*{re.escape(repr(value))}",
    ):
        _variant_config(
            tmp_path, krand=4, variants={"ens": {"realizations": value}}
        ).resolve()


@pytest.mark.parametrize("krand", [0, 1, 2, 3, 12])
def test_several_realizations_require_krand_4_or_a_seed(tmp_path, krand):
    config = _variant_config(tmp_path, krand=krand, variants={"e": {"realizations": 3}})
    with pytest.raises(ValueError, match="requires krand=4 or krand=2 with a seed"):
        config.resolve()


def test_several_realizations_accept_krand_4_or_seeded_krand_2(tmp_path):
    a = _variant_config(tmp_path, krand=4, variants={"e": {"realizations": 3}})
    assert a.resolve()["e"].realizations == 3
    b = _variant_config(tmp_path, krand=2, seed=7, variants={"e": {"realizations": 2}})
    assert b.resolve()["e"].realizations == 2
    with pytest.raises(
        ValueError, match="realizations must be a whole number, 1 or more"
    ):
        _variant_config(tmp_path, variants={"e": {"realizations": 0}})


def test_krand_4_realizations_each_get_their_own_seed(tmp_path):
    # HYSPLIT's development branch reseeds from SEED outside ichem=8, so
    # realizations that share a SEED would repeat one another.
    config = _variant_config(tmp_path, krand=4, variants={"e": {"realizations": 3}})
    e = config.resolve()["e"]
    assert e.transport.seed is None  # the folder's settings are unchanged
    assert [e.transport_for(k).seed for k in range(3)] == [0, 1, 2]


def test_an_ensemble_keeps_its_folder_when_its_realizations_grow(tmp_path):
    """An ensemble of one is an ensemble, so raising the count later only adds runs."""
    cfg = _variant_config(tmp_path, variants={"e": {"realizations": 1}, "single": {}})
    variants = cfg.resolve()
    assert variants["e"].realization_numbers == [0]
    assert variants["single"].realization_numbers == [None]
    # An ensemble records that it is one, so it is not the single run.
    assert variants["e"].result_hash != variants["single"].result_hash
    more = _variant_config(
        tmp_path, krand=4, variants={"e": {"realizations": 3}, "single": {}}
    ).resolve()
    one = _variant_config(
        tmp_path, krand=4, variants={"e": {"realizations": 1}, "single": {}}
    ).resolve()
    assert more["e"].result_hash == one["e"].result_hash
    assert "realization" not in variants["single"].run_settings
    assert "ensemble" not in variants["single"].run_settings
    assert variants["e"].run_settings["ensemble"] is True


def test_to_yaml_always_writes_the_variants_that_run(tmp_path):
    mc = _met_config(tmp_path)["hrrr"]
    cfg = ProjectConfig(
        **blocks(
            mets={"hrrr": mc, "gfs": mc}, numpar=50, variants={"hrrr": {}, "gfs": {}}
        )
    )
    text = cfg.to_yaml()
    assert "variants:\n  hrrr: {}\n  gfs: {}\n" in text
    loaded = ProjectConfig.model_validate(__import__("yaml").safe_load(text))
    assert list(loaded.resolve()) == ["hrrr", "gfs"]
    assert loaded.resolve()["gfs"].met == "gfs"


def test_variant_named_after_a_met_uses_it(tmp_path):
    mc = _met_config(tmp_path)["hrrr"]
    cfg = ProjectConfig(
        models={"hysplit": {"n_hours": -24}},
        mets={"hrrr": mc, "gfs": mc},
        variants={"gfs": {}, "hrrr": {}},
    )
    assert {n: v.met for n, v in cfg.resolve().items()} == {
        "gfs": "gfs",
        "hrrr": "hrrr",
    }


def test_maxpar_follows_each_variants_numpar(tmp_path):
    """A variant that raises numpar is not capped at the default's (the old validator did)."""
    cfg = _variant_config(
        tmp_path, numpar=1000, variants={"hrrr": {}, "np3k": {"numpar": 3000}}
    )
    variants = cfg.resolve()
    assert variants["hrrr"].transport.maxpar_for(1) == 1000
    assert variants["np3k"].transport.maxpar_for(1) == 3000
    assert variants["np3k"].transport.maxpar_for(40) == 40 * 76
    # An unset maxpar is recorded unset: what HYSPLIT gets depends on the
    # release's locations.
    assert variants["hrrr"].run_settings["maxpar"] is None


# ---------------------------------------------------------------------------
# Variants are declared; unknown keys name the nearest setting
# ---------------------------------------------------------------------------


def test_a_config_without_variants_says_what_to_write(tmp_path):
    with pytest.raises(
        ValueError, match=r"no variants. Add\n  variants:\n    hrrr: \{\}"
    ):
        ProjectConfig(models={"hysplit": {"n_hours": -24}}, mets=_met_config(tmp_path))


def test_a_met_may_be_only_its_folder(tmp_path):
    """``hrrr: /path/to/hrrr`` is the whole entry, and stays one line in the file."""
    write_arl_file(tmp_path / "met" / "20230101_00-05_hrrr")
    config = ProjectConfig(
        models={"hysplit": {"n_hours": -24}},
        mets={"hrrr": str(tmp_path / "met")},
        variants={"hrrr": {}},
    )

    assert config.met_config("hrrr").directory == tmp_path / "met"
    written = config.to_yaml()
    assert yaml.safe_load(written)["mets"] == {"hrrr": str(tmp_path / "met")}


def test_a_met_may_be_a_path_in_python(tmp_path):
    write_arl_file(tmp_path / "met" / "20230101_00-05_hrrr")
    config = ProjectConfig(
        models={"hysplit": {"n_hours": -24}},
        mets={"hrrr": tmp_path / "met"},
        variants={"hrrr": {}},
    )

    assert config.mets == {"hrrr": str(tmp_path / "met")}


def test_a_met_label_is_read_from_config_yaml_and_written_back(tmp_path):
    text = textwrap.dedent(
        f"""
        mets:
          wrf-mynn:
            directory: {tmp_path / "met"}
            label: mynn
        models:
          hysplit:
            n_hours: -24
        variants:
          wrf-mynn: {{}}
        """
    )
    path = tmp_path / "config.yaml"
    path.write_text(text)

    config = ProjectConfig.from_yaml(path)

    assert config.met_config("wrf-mynn").label == "mynn"
    assert yaml.safe_load(config.to_yaml())["mets"]["wrf-mynn"]["label"] == "mynn"


def test_a_met_label_is_not_an_archive_option(tmp_path):
    from runtraj.models.hysplit import HysplitMetConfig

    met = HysplitMetConfig(directory=tmp_path, download="hrrr", label="mine")

    assert met.label == "mine"
    assert met.download_options == {}


def test_a_relative_met_folder_starts_from_the_project(tmp_path):
    write_arl_file(tmp_path / "met" / "20230101_00-05_hrrr")
    config = ProjectConfig(
        models={"hysplit": {"n_hours": -24}},
        mets={"hrrr": "met"},
        variants={"hrrr": {}},
    )

    assert config.resolve(tmp_path)["hrrr"].met_config.directory == tmp_path / "met"


@pytest.mark.parametrize("readme", [False, True], ids=["no-folder", "no-arl-file"])
def test_a_met_whose_files_cannot_be_found_is_a_meteorology_error(tmp_path, readme):
    from runtraj.exceptions import MeteorologyError
    from runtraj.models.hysplit import HysplitMetConfig

    directory = tmp_path / "missing"
    if readme:
        directory.mkdir()
        (directory / "README").write_text("not an ARL file")
    cfg = ProjectConfig(
        **blocks(
            mets={"hrrr": HysplitMetConfig(directory=directory)}, variants={"hrrr": {}}
        )
    )
    variant = cfg.resolve()["hrrr"]
    with pytest.raises(MeteorologyError, match="Met 'hrrr'") as error:
        _ = variant.met_settings
    assert isinstance(error.value, FileNotFoundError)

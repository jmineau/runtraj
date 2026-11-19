"""Test basic functionality of runtraj."""

import runtraj


def test_version():
    """Test that version is defined."""
    assert hasattr(runtraj, "__version__")
    assert isinstance(runtraj.__version__, str)


def test_the_top_level_is_what_a_user_calls():
    """Every name in runtraj.__all__ imports, and the plumbing stays in its module."""
    for name in runtraj.__all__:
        assert hasattr(runtraj, name), name
    for name in ("Output", "Met", "Variant", "SimID", "Geometry", "write_particles"):
        assert not hasattr(runtraj, name), name


def test_the_old_names_are_gone():
    """Model and its collections were replaced by Project; no aliases in an alpha."""
    for name in ("Model", "ModelConfig"):
        assert not hasattr(runtraj, name), name

"""
Classes named by import path: a transport model or a stage in another package.

``config.yaml`` names such a class as ``class: mypkg.models.MyModel``. No
registration is needed, so every process that opens the project finds the
class, provided its package is installed there.
"""

from __future__ import annotations

import difflib
import importlib
from collections.abc import Iterable
from typing import Any

from runtraj.exceptions import MissingClass


def import_class(path: str) -> Any:
    """
    Return the class at the import path *path*, such as ``mypkg.models.MyModel``.

    Raises
    ------
    MissingClass
        If the class's package is not installed here, or the package has no
        such class. The message names what to install.
    """
    module_name, _, attr = path.rpartition(".")
    package = module_name.partition(".")[0]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name != package:
            raise MissingClass(
                f"config.yaml names {path}, which could not be imported "
                f"({error}). Check the path, and the version of {package} "
                "installed here."
            ) from None
        raise MissingClass(
            f"config.yaml names {path}, but {package} is not installed here. "
            f"Install the package that provides {package} and run this again."
        ) from None
    except ImportError as error:
        raise MissingClass(
            f"config.yaml names {path}, which could not be imported ({error})."
        ) from None
    try:
        return getattr(module, attr)
    except AttributeError:
        raise MissingClass(
            f"config.yaml names {path}, but {module_name} has no {attr}. Check "
            f"the path, and the version of {package} installed here."
        ) from None


def near_miss(cls: type, hooks: Iterable[str]) -> str | None:
    """
    Return what to say of a public name *cls* defines that is spelled almost like one of *hooks*, or ``None``.

    A misspelled hook, such as ``release_row`` for ``release_rows``, is a
    method runtraj never calls: the base class's default runs in its place.
    Only the names *cls* itself defines are looked at. A helper can be
    spelled like a hook on purpose (``check_release`` beside
    ``check_releases``), so this is for a plug-in's tests to report
    (``check_model``, ``check_stage``), never a reason to refuse a class.
    """
    hooks = set(hooks)
    for attr in vars(cls):
        if attr.startswith("_") or attr in hooks:
            continue
        close = difflib.get_close_matches(attr, hooks, n=1, cutoff=0.85)
        if close:
            return (
                f"{cls.__qualname__}.{attr} is spelled almost like the hook "
                f"{close[0]!r}, which runtraj calls instead. Rename it {close[0]} "
                "to override the hook, or give it another name."
            )
    return None


__all__ = ["import_class", "near_miss"]

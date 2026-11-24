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


def refuse_near_misses(cls: type, hooks: Iterable[str], kind: str) -> None:
    """
    Refuse a public name of *cls* that is not one of *hooks* but is spelled almost like one.

    A misspelled hook, such as ``release_row`` for ``release_rows``, would
    otherwise be a method runtraj never calls, and the base class's default
    would run in its place. Only the names *cls* itself defines are
    checked, so a subclass's own helpers with other names are left alone.

    Raises
    ------
    TypeError
        Naming the attribute and the hook it is spelled like.
    """
    hooks = set(hooks)
    for attr in vars(cls):
        if attr.startswith("_") or attr in hooks:
            continue
        close = difflib.get_close_matches(attr, hooks, n=1, cutoff=0.85)
        if close:
            raise TypeError(
                f"{cls.__qualname__}.{attr} is spelled almost like the {kind} "
                f"hook {close[0]!r}, which runtraj would call instead. Rename it "
                f"{close[0]} to override the hook, or give it another name."
            )


__all__ = ["import_class", "refuse_near_misses"]

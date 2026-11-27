"""The transport models runtraj supports, and how a model is found by name."""

from __future__ import annotations

from collections.abc import Callable

from runtraj._plugins import import_class
from runtraj.models.base import MetConfig, TransportModel


def _hysplit() -> TransportModel:
    """Return HYSPLIT, importing its package only when it is asked for."""
    from runtraj.models.hysplit import HysplitModel

    return HysplitModel()


def _flexpart() -> TransportModel:
    """Return FLEXPART, importing its package only when it is asked for."""
    from runtraj.models.flexpart import FlexpartModel

    return FlexpartModel()


#: The transport models runtraj supports, by the name of their block under
#: ``models:`` in ``config.yaml``: ``hysplit``, and ``flexpart``, which runs
#: a FLEXPART 11 checkout you built. Any other model is named
#: by its import path instead, the ``class`` of its block.
MODELS: dict[str, Callable[[], TransportModel]] = {
    "hysplit": _hysplit,
    "flexpart": _flexpart,
}


def get_model(name: str) -> TransportModel:
    """
    Return the transport model *name* names.

    *name* is a model runtraj supports (:data:`MODELS`, such as ``hysplit``) or the
    import path of a model class, such as ``emulator.runtraj.EmulatorModel``,
    which is imported and made with no arguments. A model in a package of
    its own needs no registration, so every process that opens the project
    finds it.

    Raises
    ------
    ValueError
        If *name* is not a model runtraj supports and not an import path, or the
        class at the import path does not subclass :class:`TransportModel`
        or lacks one of its required members.
    MissingClass
        If *name* is an import path that cannot be imported on this machine
        (an :class:`ImportError`).
    """
    if "." in name:
        cls = import_class(name)
        if not (isinstance(cls, type) and issubclass(cls, TransportModel)):
            raise ValueError(
                f"{name} is not a transport model: its class must subclass "
                "runtraj.models.TransportModel."
            )
        missing = [
            member
            for member in ("name", "config_class", "met_config_class")
            if not hasattr(cls, member)
        ] + sorted(cls.__abstractmethods__)
        if missing:
            raise ValueError(
                f"{name} is not a transport model: it has no {', '.join(missing)}."
            )
        met_class = cls.met_config_class
        if not (isinstance(met_class, type) and issubclass(met_class, MetConfig)):
            raise ValueError(
                f"{name}'s met_config_class must subclass runtraj.models.MetConfig."
            )
        return cls()
    factory = MODELS.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown transport model {name!r}. The models runtraj supports are "
            f"{sorted(MODELS)}; another is named by its import path, such as "
            "mypkg.models.MyModel."
        )
    return factory()

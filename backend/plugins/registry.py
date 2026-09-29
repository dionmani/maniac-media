"""Dynamic plugin discovery.

Sources, in order:
  1. Built-in packages ``backend.plugins.<kind>s`` (every module is imported).
  2. Python files / packages dropped into ``data/plugins`` (third-party, no rebuild needed).
  3. Installed distributions exposing the ``maniac_media.plugins`` entry point group.

Anything subclassing a plugin contract and decorated with :func:`register` becomes available
by its ``key``. A broken third-party plugin is logged and skipped – it never takes the core down.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
import sys
from importlib.metadata import entry_points
from pathlib import Path
from typing import TypeVar

from backend.core import paths
from backend.plugins.base import Plugin, PluginKind

log = logging.getLogger("maniac.plugins")

P = TypeVar("P", bound=type[Plugin])

_registry: dict[PluginKind, dict[str, type[Plugin]]] = {k: {} for k in PluginKind}
_load_errors: list[dict[str, str]] = []
_discovered = False

BUILTIN_PACKAGES = {
    PluginKind.METADATA: "backend.plugins.metadata",
    PluginKind.INDEXER: "backend.plugins.indexers",
    PluginKind.DOWNLOAD_CLIENT: "backend.plugins.download_clients",
    PluginKind.NOTIFICATION: "backend.plugins.notifications",
}


def register(cls: P) -> P:
    kind = cls.kind
    existing = _registry[kind].get(cls.key)
    if existing is not None and existing is not cls:
        log.warning("Plugin key '%s' (%s) overridden by %s", cls.key, existing.__module__, cls.__module__)
    _registry[kind][cls.key] = cls
    return cls


def discover(force: bool = False) -> None:
    global _discovered
    if _discovered and not force:
        return
    _discovered = True
    _load_errors.clear()

    for package_name in BUILTIN_PACKAGES.values():
        package = importlib.import_module(package_name)
        for mod in pkgutil.iter_modules(package.__path__):
            _safe_import(f"{package_name}.{mod.name}")

    plugin_dir = paths.PLUGIN_DIR
    if plugin_dir.exists():
        for entry in sorted(plugin_dir.iterdir()):
            if entry.name.startswith(("_", ".")):
                continue
            if entry.suffix == ".py" or (entry.is_dir() and (entry / "__init__.py").exists()):
                _load_external(entry)

    try:
        for ep in entry_points(group="maniac_media.plugins"):
            try:
                obj = ep.load()
                if isinstance(obj, type) and issubclass(obj, Plugin):
                    obj.builtin = False
                    register(obj)
            except Exception as exc:
                _record_error(ep.name, exc)
    except Exception:  # pragma: no cover
        pass

    log.info("Plugins loaded: %s", {k.value: sorted(v) for k, v in _registry.items()})


def _safe_import(module: str) -> None:
    try:
        importlib.import_module(module)
    except Exception as exc:
        _record_error(module, exc)


def _load_external(path: Path) -> None:
    name = f"maniac_ext_{path.stem}"
    try:
        target = path / "__init__.py" if path.is_dir() else path
        spec = importlib.util.spec_from_file_location(name, target)
        if spec is None or spec.loader is None:
            raise ImportError("no loader")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        before = {k: set(v) for k, v in _registry.items()}
        spec.loader.exec_module(module)
        for kind, keys in _registry.items():
            for key in set(keys) - before[kind]:
                keys[key].builtin = False
    except Exception as exc:
        _record_error(str(path), exc)


def _record_error(source: str, exc: Exception) -> None:
    log.error("Failed to load plugin %s: %s", source, exc)
    _load_errors.append({"source": source, "error": f"{type(exc).__name__}: {exc}"})


def get(kind: PluginKind, key: str) -> type[Plugin]:
    discover()
    try:
        return _registry[kind][key]
    except KeyError:
        raise KeyError(f"No {kind.value} plugin named '{key}'") from None


def available(kind: PluginKind) -> dict[str, type[Plugin]]:
    discover()
    return dict(_registry[kind])


def catalogue() -> dict:
    discover()
    return {
        "plugins": {k.value: [cls.describe() for cls in sorted(v.values(), key=lambda c: c.name)] for k, v in _registry.items()},
        "errors": list(_load_errors),
        "plugin_dir": str(paths.PLUGIN_DIR),
    }

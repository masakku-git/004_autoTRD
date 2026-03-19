"""戦略プラグインの自動発見・登録（plugins/ディレクトリのBaseStrategy子クラスを自動読み込み）"""
from __future__ import annotations

import importlib
import pathlib
import pkgutil

from src.strategy.base import BaseStrategy
from src.utils.logger import logger

_registry: dict[str, type[BaseStrategy]] = {}


def discover_strategies() -> None:
    """Scan plugins/ directory and register all BaseStrategy subclasses."""
    global _registry
    _registry.clear()

    plugins_dir = pathlib.Path(__file__).parent / "plugins"
    if not plugins_dir.exists():
        logger.warning(f"Plugins directory not found: {plugins_dir}")
        return

    for importer, modname, ispkg in pkgutil.iter_modules([str(plugins_dir)]):
        if modname.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"src.strategy.plugins.{modname}")
            for attr_name in dir(module):
                obj = getattr(module, attr_name)
                if (
                    isinstance(obj, type)
                    and issubclass(obj, BaseStrategy)
                    and obj is not BaseStrategy
                ):
                    _registry[obj.name] = obj
                    logger.info(f"Registered strategy: {obj.name} v{obj.version}")
        except Exception as e:
            logger.error(f"Failed to load plugin {modname}: {e}")

    logger.info(f"Total strategies registered: {len(_registry)}")


def get_strategy(name: str) -> BaseStrategy:
    """Get a strategy instance by name."""
    if name not in _registry:
        raise KeyError(f"Strategy '{name}' not found. Available: {list(_registry.keys())}")
    return _registry[name]()


def get_strategies_for_regime(regime: str) -> list[BaseStrategy]:
    """Get all strategy instances matching the given market regime."""
    return [
        cls()
        for cls in _registry.values()
        if cls.target_regime in (regime, "any")
    ]


def list_strategies() -> list[dict]:
    """List all registered strategies with metadata."""
    return [
        {
            "name": cls.name,
            "version": cls.version,
            "target_regime": cls.target_regime,
        }
        for cls in _registry.values()
    ]

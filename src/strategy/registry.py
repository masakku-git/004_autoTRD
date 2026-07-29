"""戦略プラグインの自動発見・登録（plugins/ディレクトリのBaseStrategy子クラスを自動読み込み）"""
from __future__ import annotations

import importlib
import pathlib
import pkgutil

from src.strategy.base import BaseStrategy
from src.utils.logger import logger

_registry: dict[str, type[BaseStrategy]] = {}


def discover_strategies() -> int:
    """Scan plugins/ directory and register all BaseStrategy subclasses.

    Returns the number of registered strategies. 0件は「BUY/戦略ベースSELLが
    一切動かないのに日次レポートは正常に見える」状態なのでSlackへ通知する。
    """
    global _registry
    _registry.clear()

    plugins_dir = pathlib.Path(__file__).parent / "plugins"
    if plugins_dir.exists():
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
    else:
        logger.warning(f"Plugins directory not found: {plugins_dir}")

    count = len(_registry)
    logger.info(f"Total strategies registered: {count}")
    if count == 0:
        # 循環import回避のため遅延import（broker/account.pyと同パターン）
        from src.notify.notifier import send_notification

        send_notification(
            "戦略プラグイン読み込み失敗",
            "戦略が1件も登録されませんでした（plugins/全滅）。\n"
            "影響: 新規BUY・戦略ベースのSELL判定は本日実行されません。\n"
            "SL/利確/最大保有期間による強制エグジットはデフォルトロジックで継続します。\n"
            "対応: logs で 'Failed to load plugin' を確認してください。",
            level="error",
        )
    return count


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

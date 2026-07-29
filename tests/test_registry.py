"""Tests for strategy discovery failure detection."""
import pkgutil

import src.notify.notifier as notifier
from src.strategy.registry import discover_strategies


def test_discover_registers_plugins():
    count = discover_strategies()
    assert count > 0


def test_zero_registrations_sends_notification(monkeypatch):
    notifications = []
    monkeypatch.setattr(
        notifier,
        "send_notification",
        lambda title, message, level="info": notifications.append((title, level)) or True,
    )
    monkeypatch.setattr(pkgutil, "iter_modules", lambda paths: [])

    count = discover_strategies()
    assert count == 0
    assert len(notifications) == 1
    assert notifications[0][1] == "error"

    # Restore the real registry for other tests
    monkeypatch.undo()
    assert discover_strategies() > 0

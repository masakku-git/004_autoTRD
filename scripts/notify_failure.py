"""systemd の OnFailure= から呼ばれ、ユニット失敗を Slack に通知する。

使い方: python scripts/notify_failure.py <unit名>   （例: autotrader.service）
run_daily 内の例外は main.py の except で通知されるが、Python 起動前・import失敗・
強制終了など「プロセスが通知まで辿り着けない」失敗をここで拾う。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.notify.notifier import send_notification


def main() -> int:
    unit = sys.argv[1] if len(sys.argv) > 1 else "(unknown unit)"
    send_notification(
        f"systemdユニット失敗: {unit}",
        f"{unit} が異常終了しました。\n"
        f"詳細: journalctl -u {unit} -n 100\n"
        "日次実行(autotrader.service)の場合、損切り・売却・新規エントリーが実行されていない可能性があります。",
        level="error",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

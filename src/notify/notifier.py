"""Slack Webhook通知（日次レポート・警告・エラーをSlackチャンネルに送信）"""
import requests

from config.settings import settings
from src.utils.logger import logger


def send_notification(title: str, message: str, level: str = "info") -> bool:
    """Slack Incoming Webhookでメッセージを送信する。

    Args:
        title: 通知タイトル
        message: 通知本文
        level: "info", "warning", "error"
    """
    # レベルに応じた絵文字プレフィックス
    emoji = {"info": ":chart_with_upwards_trend:", "warning": ":warning:", "error": ":rotating_light:"}.get(level, "")
    full_message = f"{emoji} *{title}*\n```\n{message}\n```"

    # Webhook URLが未設定の場合はログ出力のみ
    if not settings.slack_webhook_url:
        logger.info(f"Notification (no webhook): {title}\n{message}")
        return False

    try:
        resp = requests.post(
            settings.slack_webhook_url,
            json={"text": full_message},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info(f"Notification sent: {title}")
            return True
        else:
            logger.error(f"Slack webhook failed: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Notification error: {e}")
        return False

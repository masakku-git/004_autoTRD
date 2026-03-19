"""Notification system (LINE Notify)."""
import requests

from config.settings import settings
from src.utils.logger import logger

LINE_NOTIFY_API = "https://notify-api.line.me/api/notify"


def send_notification(title: str, message: str, level: str = "info") -> bool:
    """Send notification via LINE Notify.

    Args:
        title: Notification title
        message: Notification body
        level: "info", "warning", or "error"
    """
    prefix = {"info": "", "warning": "[WARN] ", "error": "[ERROR] "}.get(level, "")
    full_message = f"\n{prefix}{title}\n{message}"

    if not settings.line_notify_token:
        logger.info(f"Notification (no token): {full_message}")
        return False

    try:
        resp = requests.post(
            LINE_NOTIFY_API,
            headers={"Authorization": f"Bearer {settings.line_notify_token}"},
            data={"message": full_message},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info(f"Notification sent: {title}")
            return True
        else:
            logger.error(f"LINE Notify failed: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Notification error: {e}")
        return False

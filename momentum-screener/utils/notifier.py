"""
Telegram 推播工具
"""
import os
import httpx
import logging

logger = logging.getLogger(__name__)

BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
_BASE      = f"https://api.telegram.org/bot{BOT_TOKEN}"


async def send_message(text: str, chat_id: str = "") -> bool:
    if not BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN 未設定")
        return False
    target = chat_id or CHAT_ID
    if not target:
        logger.warning("TELEGRAM_CHAT_ID 未設定")
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{_BASE}/sendMessage", json={
                "chat_id":    target,
                "text":       text,
                "parse_mode": "Markdown",
            })
            return r.status_code == 200
    except Exception as e:
        logger.error(f"Telegram 推播失敗: {e}")
        return False


async def send_photo(
    image_bytes: bytes,
    caption: str = "",
    chat_id: str = "",
) -> bool:
    """傳送圖片到 Telegram。"""
    if not BOT_TOKEN:
        return False
    target = chat_id or CHAT_ID
    if not target:
        return False
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{_BASE}/sendPhoto",
                data={"chat_id": target, "caption": caption, "parse_mode": "Markdown"},
                files={"photo": ("chart.png", image_bytes, "image/png")},
            )
            if r.status_code != 200:
                logger.warning(f"Telegram sendPhoto 失敗: {r.text[:200]}")
            return r.status_code == 200
    except Exception as e:
        logger.error(f"Telegram 圖片推播失敗: {e}")
        return False


async def set_webhook(url: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{_BASE}/setWebhook", json={"url": url})
        return r.json()


async def get_me() -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{_BASE}/getMe")
            return r.json()
    except Exception:
        return {}

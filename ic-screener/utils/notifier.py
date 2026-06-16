"""
Telegram 推播（共用同一個 Bot Token）
"""
import os
import httpx
import logging

logger   = logging.getLogger(__name__)
TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
_BASE    = f"https://api.telegram.org/bot{TOKEN}"


async def send(text: str, chat_id: str = "") -> bool:
    if not TOKEN:
        return False
    target = chat_id or CHAT_ID
    if not target:
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

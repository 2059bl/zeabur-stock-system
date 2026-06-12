import os
import logging
import httpx

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "8141519967:AAFodUHthSpQsFTN_4E4iUdm7tPEn_Sb9jE",
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

_URL = "https://api.telegram.org/bot{token}/sendMessage"


async def send_telegram(message: str) -> bool:
    if not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_CHAT_ID 未設定，跳過推播")
        return False

    url = _URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("Telegram 推播成功")
                return True
            logger.error(f"Telegram 錯誤 {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Telegram 推播例外: {e}")
        return False


async def get_chat_id() -> dict:
    """呼叫 getUpdates 幫助查詢 chat_id（對 bot 發訊息後使用）"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            data = resp.json()
            results = data.get("result", [])
            if not results:
                return {"hint": "請先對 bot 發送任意訊息，再呼叫此端點"}
            chats = [
                {
                    "chat_id": r["message"]["chat"]["id"],
                    "from": r["message"]["from"].get("username", ""),
                    "text": r["message"].get("text", ""),
                }
                for r in results if "message" in r
            ]
            return {"chats": chats}
    except Exception as e:
        return {"error": str(e)}

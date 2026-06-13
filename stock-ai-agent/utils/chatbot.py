"""
Telegram 雙向聊天機器人
使用 OpenRouter API，支援繁體中文對話、股票查詢、選股指令。
"""
import os
import json
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

OR_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OR_MODEL   = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3.5-haiku")
BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")

_OR_URL    = "https://openrouter.ai/api/v1/chat/completions"
_TG_URL    = "https://api.telegram.org/bot{token}/{method}"

_SYSTEM_PROMPT = """你是「台股 AI 量化助理」，一個專業的台股分析客服機器人。
你的功能：
1. 回答台股相關問題（技術分析、籌碼、產業趨勢）
2. 告訴使用者目前系統追蹤的股票與指標
3. 解釋空頭排列、RSI、MACD 等指標意涵
4. 根據使用者提供的股票資料給出簡要分析意見

回覆規則：
- 使用繁體中文
- 語氣專業但親切，像財經顧問
- 回覆簡潔，重點優先，不超過 300 字
- 涉及個股買賣建議時，加上「僅供參考，請自行判斷風險」
- 如果問到系統資料（追蹤哪些股票、今日指標），告知使用者可以用以下指令查詢：
  /stocks - 查看追蹤股票清單
  /screen - 查看空頭候選
  /help - 查看所有指令

不要做的事：
- 不提供具體進出場時機的明確指令
- 不保證報酬率
- 不回答與台股、投資無關的問題（禮貌拒絕）"""


async def send_message(chat_id: str, text: str, parse_mode: str = "Markdown") -> bool:
    url = _TG_URL.format(token=BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=payload)
            return r.status_code == 200
    except Exception as e:
        logger.error(f"Telegram 發訊失敗: {e}")
        return False


async def chat_with_llm(user_message: str, context: Optional[str] = None) -> str:
    if not OR_API_KEY:
        return "⚠️ OpenRouter API Key 未設定，無法回覆。"

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    if context:
        messages.append({"role": "system", "content": f"[系統資料補充]\n{context}"})
    messages.append({"role": "user", "content": user_message})

    headers = {
        "Authorization": f"Bearer {OR_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://twstock-agent-1781283629.zeabur.app",
    }
    payload = {
        "model": OR_MODEL,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": 600,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(_OR_URL, json=payload, headers=headers)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"OpenRouter 失敗: {e}")
        return "抱歉，AI 暫時無法回應，請稍後再試。"


async def set_webhook(webhook_url: str) -> dict:
    url = _TG_URL.format(token=BOT_TOKEN, method="setWebhook")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, json={"url": webhook_url, "allowed_updates": ["message"]})
        return r.json()


async def delete_webhook() -> dict:
    url = _TG_URL.format(token=BOT_TOKEN, method="deleteWebhook")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, json={})
        return r.json()

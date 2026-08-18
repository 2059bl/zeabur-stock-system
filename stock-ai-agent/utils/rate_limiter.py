"""
API 限流器 — 防崩潰核心
========================
1. asyncio.Semaphore：最多 5 個並發 FinMind 請求
2. 每日 API 呼叫計數（寫入 DB），超過 8000 停止並告警
3. 指數退避重試（最多 3 次，間隔 2/4/8 秒）
"""
import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 全局並發限制：同一時間最多 5 個 FinMind 請求
_FINMIND_SEM = asyncio.Semaphore(5)

# 每日上限
DAILY_LIMIT = 8000

# 本進程的計數器（跨進程的寫 DB，這個是進程內快速檢查）
_daily_count: dict[str, int] = {}
_daily_date: Optional[str] = None


def _today() -> str:
    from datetime import date
    return str(date.today())


def _reset_if_new_day():
    global _daily_date, _daily_count
    today = _today()
    if _daily_date != today:
        _daily_date = today
        _daily_count = {}


def _increment(api: str = "finmind", n: int = 1) -> int:
    _reset_if_new_day()
    _daily_count[api] = _daily_count.get(api, 0) + n
    return _daily_count[api]


def get_today_count(api: str = "finmind") -> int:
    _reset_if_new_day()
    return _daily_count.get(api, 0)


def is_over_limit(api: str = "finmind") -> bool:
    return get_today_count(api) >= DAILY_LIMIT


async def finmind_get(client, url: str, params: dict, retries: int = 3) -> dict:
    """
    受限流器保護的 FinMind GET。
    - 並發上限：5
    - 每日上限：8000 calls
    - 失敗自動重試 3 次，指數退避
    """
    if is_over_limit():
        logger.error(f"⛔ FinMind 今日已達 {DAILY_LIMIT} 次上限，停止請求")
        return {}

    async with _FINMIND_SEM:
        _increment("finmind")
        for attempt in range(retries):
            try:
                r = await client.get(url, params=params, timeout=20)
                if r.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"FinMind 429 rate limit，等待 {wait}s (attempt {attempt+1})")
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                data = r.json()
                if data.get("status") == 200:
                    return data
                # status != 200 但不是 HTTP 錯誤
                msg = data.get("msg", "")
                if "over" in msg.lower() or "limit" in msg.lower():
                    logger.warning(f"FinMind API 限流訊息：{msg}")
                    await asyncio.sleep(5)
                    continue
                logger.warning(f"FinMind 非200回應：{msg}")
                return data
            except asyncio.TimeoutError:
                wait = 2 ** attempt
                logger.warning(f"FinMind timeout，等待 {wait}s (attempt {attempt+1})")
                await asyncio.sleep(wait)
            except Exception as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"FinMind 錯誤 {e}，等待 {wait}s (attempt {attempt+1})")
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"FinMind 最終失敗：{e}")
        return {}

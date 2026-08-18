"""
FinMind API 客戶端 — 帶完整 quota 管理、rate-limit 防護、詳細日誌
FinMind 付費方案：~8000 requests/day，超過自動停止
"""
import asyncio
import logging
import time
import os
from datetime import date, datetime
from typing import Optional

import httpx

from src.db import execute, fetch_one

logger = logging.getLogger(__name__)

FINMIND_TOKEN = os.environ["FINMIND_TOKEN"]
FM_BASE = "https://api.finmindtrade.com/api/v4/data"

DAILY_LIMIT   = int(os.environ.get("FINMIND_DAILY_LIMIT", "8000"))
SAFE_THRESHOLD = int(os.environ.get("FINMIND_SAFE_THRESHOLD", "7000"))
CONCURRENCY   = int(os.environ.get("FINMIND_CONCURRENCY", "5"))
BROKER_CONCURRENCY = int(os.environ.get("BROKER_CONCURRENCY", "3"))
RETRY_MAX     = int(os.environ.get("FINMIND_RETRY_MAX", "3"))
TIMEOUT       = int(os.environ.get("FINMIND_TIMEOUT", "20"))

_sem        = asyncio.Semaphore(CONCURRENCY)
_broker_sem = asyncio.Semaphore(BROKER_CONCURRENCY)

# 進程內計數（快速檢查，DB 為持久化）
_proc_count: dict[str, int] = {}
_proc_date: Optional[str] = None


def _reset_if_new_day():
    global _proc_date, _proc_count
    today = str(date.today())
    if _proc_date != today:
        _proc_date = today
        _proc_count = {}


def _inc(api: str = "finmind", n: int = 1) -> int:
    _reset_if_new_day()
    _proc_count[api] = _proc_count.get(api, 0) + n
    return _proc_count[api]


def is_over_limit(api: str = "finmind") -> bool:
    _reset_if_new_day()
    return _proc_count.get(api, 0) >= DAILY_LIMIT


def is_near_limit(api: str = "finmind") -> bool:
    _reset_if_new_day()
    return _proc_count.get(api, 0) >= SAFE_THRESHOLD


def get_today_count(api: str = "finmind") -> int:
    _reset_if_new_day()
    return _proc_count.get(api, 0)


async def _log_request(
    dataset: str,
    stock_code: Optional[str],
    query_date: Optional[str],
    http_status: int,
    api_status: int,
    row_count: int,
    rate_limited: bool = False,
    api_error: bool = False,
    error_msg: str = "",
    response_ms: int = 0,
):
    try:
        await execute(
            """
            INSERT INTO api_request_log
              (provider, dataset, stock_code, query_date,
               http_status, api_status, row_count,
               rate_limited, api_error, error_message, response_ms)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """,
            "finmind", dataset, stock_code,
            date.fromisoformat(query_date) if query_date else None,
            http_status, api_status, row_count,
            rate_limited, api_error, error_msg or None, response_ms,
        )
    except Exception as e:
        logger.debug(f"API log insert failed: {e}")


async def fm_get(
    dataset: str,
    data_id: Optional[str],
    start_date: str,
    end_date: Optional[str] = None,
    use_broker_sem: bool = False,
) -> list[dict]:
    """
    FinMind GET 請求，帶完整保護機制。
    - is_broker=True 時使用分點專用並發限制（更低）
    - 回傳 list[dict]，失敗回傳 []
    """
    if is_over_limit():
        logger.error("⛔ FinMind 今日已達上限，停止請求")
        return []

    sem = _broker_sem if use_broker_sem else _sem

    params: dict = {
        "dataset": dataset,
        "start_date": start_date,
        "token": FINMIND_TOKEN,
    }
    if data_id:
        params["data_id"] = data_id
    if end_date:
        params["end_date"] = end_date

    async with sem:
        _inc("finmind")
        t0 = time.monotonic()

        for attempt in range(RETRY_MAX):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                    r = await c.get(FM_BASE, params=params)

                elapsed_ms = int((time.monotonic() - t0) * 1000)

                if r.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    logger.warning(f"FinMind 429 rate limit {dataset}/{data_id}, wait {wait}s")
                    await _log_request(dataset, data_id, start_date, 429, -1, 0,
                                       rate_limited=True, response_ms=elapsed_ms)
                    await asyncio.sleep(wait)
                    continue

                r.raise_for_status()
                body = r.json()
                status = body.get("status", -1)
                rows   = body.get("data", []) if status == 200 else []
                msg    = body.get("msg", "")

                rate_hit = "over" in msg.lower() or "limit" in msg.lower()
                is_err   = status != 200 and not rate_hit

                await _log_request(dataset, data_id, start_date,
                                   r.status_code, status, len(rows),
                                   rate_limited=rate_hit, api_error=is_err,
                                   error_msg=msg if is_err else "",
                                   response_ms=elapsed_ms)

                if rate_hit:
                    logger.warning(f"FinMind API quota msg: {msg}")
                    await asyncio.sleep(5)
                    continue

                if is_err:
                    logger.warning(f"FinMind {dataset}/{data_id} status={status}: {msg}")
                    return []

                return rows

            except asyncio.TimeoutError:
                wait = 2 ** attempt
                logger.warning(f"FinMind timeout {dataset}/{data_id}, wait {wait}s")
                await asyncio.sleep(wait)
            except Exception as e:
                if attempt < RETRY_MAX - 1:
                    wait = 2 ** attempt
                    logger.warning(f"FinMind error {dataset}/{data_id}: {e}, wait {wait}s")
                    await asyncio.sleep(wait)
                else:
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    await _log_request(dataset, data_id, start_date, -1, -1, 0,
                                       api_error=True, error_msg=str(e), response_ms=elapsed_ms)
                    logger.error(f"FinMind 最終失敗 {dataset}/{data_id}: {e}")

        return []


async def get_trading_calendar(year_month: str) -> list[str]:
    """確認交易日：用 0050 行情推算交易日曆。"""
    rows = await fm_get("TaiwanStockPrice", "0050", f"{year_month}-01")
    dates = sorted({r["date"] for r in rows})
    return dates


async def verify_trading_dates(dates: list[str]) -> dict[str, bool]:
    """驗證多個日期是否為交易日。"""
    if not dates:
        return {}
    min_date = min(dates)
    rows = await fm_get("TaiwanStockPrice", "0050", min_date)
    trading = {r["date"] for r in rows}
    return {d: d in trading for d in dates}

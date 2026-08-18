"""
個股日行情 — Cache-first，DB 有資料就不打 API
"""
import logging
from datetime import date, timedelta

from src.db import execute, fetch_all, fetch_one
from src.finmind_client import fm_get

logger = logging.getLogger(__name__)


async def _get_cached_price(stock_code: str, trade_date: str) -> dict | None:
    return await fetch_one(
        "SELECT * FROM stock_daily WHERE stock_code=$1 AND trade_date=$2",
        stock_code, date.fromisoformat(trade_date),
    )


async def fetch_price(stock_code: str, trade_date: str) -> dict | None:
    """取得個股當日行情（DB-first）。"""
    cached = await _get_cached_price(stock_code, trade_date)
    if cached:
        return cached

    # 拉近30天資料，計算 20日均量
    start = (date.fromisoformat(trade_date) - timedelta(days=45)).isoformat()
    rows = await fm_get("TaiwanStockPrice", stock_code, start)
    if not rows:
        return None

    # 建立日期→價格 map
    price_map = {r["date"]: r for r in rows}

    # 計算 20日均量
    sorted_dates = sorted(price_map.keys())
    for i, d in enumerate(sorted_dates):
        r = price_map[d]
        past = sorted_dates[max(0, i-19):i+1]
        avg_vol = sum(int(price_map[pd].get("Trading_Volume", 0) or 0) for pd in past) // len(past)
        vol = int(r.get("Trading_Volume", 0) or 0)
        vol_ratio = round(vol / avg_vol, 2) if avg_vol > 0 else None
        close = float(r.get("close", 0) or 0)
        prev_close = float(price_map[sorted_dates[i-1]].get("close", 1) or 1) if i > 0 else close
        change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else None

        # upsert
        try:
            await execute(
                """
                INSERT INTO stock_daily
                  (trade_date, stock_code, open_price, high_price, low_price, close_price,
                   change_pct, volume, avg_20d_vol, volume_ratio)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT(trade_date, stock_code) DO NOTHING
                """,
                date.fromisoformat(d), stock_code,
                float(r.get("open", 0) or 0), float(r.get("max", 0) or 0),
                float(r.get("min", 0) or 0), close,
                change_pct, vol, avg_vol, vol_ratio,
            )
        except Exception as e:
            logger.debug(f"stock_daily upsert {stock_code} {d}: {e}")

    return await _get_cached_price(stock_code, trade_date)


async def fetch_prices_batch(codes: list[str], trade_date: str) -> dict[str, dict | None]:
    """批量取得行情（先查 DB，缺失再打 API）。"""
    import asyncio

    result = {}
    need_fetch = []

    for code in codes:
        cached = await _get_cached_price(code, trade_date)
        if cached:
            result[code] = cached
        else:
            need_fetch.append(code)

    if need_fetch:
        logger.info(f"行情需從 API 補充：{len(need_fetch)} 支")
        tasks = [fetch_price(c, trade_date) for c in need_fetch]
        fetched = await asyncio.gather(*tasks, return_exceptions=True)
        for code, res in zip(need_fetch, fetched):
            result[code] = res if not isinstance(res, Exception) else None

    return result


async def get_crash_stocks(
    trade_date: str,
    drop_threshold: float = -4.0,
    max_count: int = 60,
) -> list[dict]:
    """從 DB 取當日跌幅超過閾值的個股（限 universe 範圍）。"""
    rows = await fetch_all(
        """
        SELECT sd.stock_code, sd.close_price, sd.change_pct, sd.volume, sd.volume_ratio,
               COALESCE(gs.stock_name, eu.etf_name) AS stock_name,
               COALESCE(gs.sector, eu.etf_type) AS sector
        FROM stock_daily sd
        LEFT JOIN growth_stock_universe gs ON sd.stock_code = gs.stock_code AND gs.active=TRUE
        LEFT JOIN etf_universe eu          ON sd.stock_code = eu.etf_code   AND eu.active=TRUE
        WHERE sd.trade_date = $1
          AND sd.change_pct <= $2
          AND (gs.stock_code IS NOT NULL OR eu.etf_code IS NOT NULL)
        ORDER BY sd.change_pct ASC
        LIMIT $3
        """,
        date.fromisoformat(trade_date), drop_threshold, max_count,
    )
    return rows

"""
融資融券資料（TaiwanStockMarginPurchaseShortSale）
"""
import logging
from datetime import date

from src.db import execute, fetch_one
from src.finmind_client import fm_get

logger = logging.getLogger(__name__)


async def fetch_margin(stock_code: str, trade_date: str) -> dict | None:
    """取得融資融券（DB-first）。"""
    cached = await fetch_one(
        """SELECT margin_balance, margin_change, short_balance, short_change
           FROM margin_daily WHERE stock_code=$1 AND trade_date=$2""",
        stock_code, date.fromisoformat(trade_date),
    )
    if cached:
        return dict(cached)

    rows = await fm_get("TaiwanStockMarginPurchaseShortSale", stock_code, trade_date)
    if not rows:
        return None

    for r in rows:
        if r.get("date") != trade_date:
            continue

        margin_bal    = int(r.get("MarginPurchaseTodayBalance") or 0)
        margin_buy    = int(r.get("MarginPurchaseBuy") or 0)
        margin_sell   = int(r.get("MarginPurchaseSell") or 0)
        margin_change = margin_buy - margin_sell
        short_bal     = int(r.get("ShortSaleTodayBalance") or 0)
        short_buy     = int(r.get("ShortSaleBuy") or 0)
        short_sell    = int(r.get("ShortSaleSell") or 0)
        short_change  = short_sell - short_buy

        try:
            await execute(
                """
                INSERT INTO margin_daily
                  (trade_date, stock_code, margin_balance, margin_buy, margin_sell,
                   margin_change, short_balance, short_change)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT(trade_date, stock_code) DO NOTHING
                """,
                date.fromisoformat(trade_date), stock_code,
                margin_bal, margin_buy, margin_sell, margin_change,
                short_bal, short_change,
            )
        except Exception as e:
            logger.debug(f"margin_daily insert {stock_code}: {e}")

        return {
            "margin_balance": margin_bal,
            "margin_change":  margin_change,
            "short_balance":  short_bal,
            "short_change":   short_change,
            "financing_data_available": True,
        }

    return None

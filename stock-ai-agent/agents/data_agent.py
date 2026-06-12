"""
data_agent — 日K報價 + FinMind 籌碼數據 抓取與寫入
支援 TWSE（上市）與 OTC（上櫃）雙來源自動切換。
"""
import httpx
import logging
from typing import Optional

from ..utils.db import execute
from ..utils.finmind_client import fetch_institutional, fetch_margin

logger = logging.getLogger(__name__)

_TWSE_URL = (
    "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
    "?response=json&date={date}&stockNo={code}"
)
_OTC_URL = (
    "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/"
    "stk_close_download.php?d={date_slash}&q={code}&s=0,asc,0&o=json"
)


def _parse_twse(data: dict) -> Optional[dict]:
    if data.get("stat") != "OK" or not data.get("data"):
        return None
    last = data["data"][-1]
    try:
        return {
            "open_price":  float(last[3].replace(",", "")),
            "high_price":  float(last[4].replace(",", "")),
            "low_price":   float(last[5].replace(",", "")),
            "close_price": float(last[6].replace(",", "")),
            "volume":      int(last[2].replace(",", "")) // 1000,
            "change_pct":  float(last[8].replace(",", "").replace("X", "") or 0),
        }
    except (ValueError, IndexError) as e:
        logger.warning(f"TWSE 解析失敗: {e}")
        return None


def _parse_otc(data: dict) -> Optional[dict]:
    records = data.get("aaData") or []
    if not records:
        return None
    last = records[-1]
    try:
        def clean(v):
            return str(v).replace(",", "").replace("--", "0").strip()
        return {
            "open_price":  float(clean(last[4])),
            "high_price":  float(clean(last[5])),
            "low_price":   float(clean(last[6])),
            "close_price": float(clean(last[2])),
            "volume":      int(float(clean(last[8]))),
            "change_pct":  float(clean(last[3]) or 0),
        }
    except (ValueError, IndexError) as e:
        logger.warning(f"OTC 解析失敗: {e}")
        return None


async def fetch_price(stock_code: str, trade_date: str, market: str = "TWSE") -> Optional[dict]:
    date_fmt = trade_date.replace("-", "")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            if market == "OTC":
                parts = trade_date.split("-")
                tw_year = int(parts[0]) - 1911
                date_slash = f"{tw_year}/{parts[1]}"
                r = await c.get(_OTC_URL.format(date_slash=date_slash, code=stock_code))
                return _parse_otc(r.json())
            else:
                r = await c.get(_TWSE_URL.format(date=date_fmt, code=stock_code))
                return _parse_twse(r.json())
    except Exception as e:
        logger.warning(f"報價抓取失敗 {stock_code} ({market}): {e}")
        return None


async def upsert_daily_prices(stock_code: str, trade_date: str, market: str = "TWSE") -> bool:
    """寫入日K報價，失敗時回傳 False。"""
    data = await fetch_price(stock_code, trade_date, market)
    if not data:
        logger.info(f"無報價數據: {stock_code} {trade_date}")
        return False

    await execute("""
        INSERT INTO stock_prices
            (stock_code, trade_date, open_price, high_price, low_price, close_price, volume, change_pct)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        ON CONFLICT (stock_code, trade_date) DO UPDATE SET
            close_price = EXCLUDED.close_price,
            volume      = EXCLUDED.volume,
            change_pct  = EXCLUDED.change_pct
    """, stock_code, trade_date,
         data["open_price"], data["high_price"], data["low_price"],
         data["close_price"], data["volume"], data["change_pct"])
    return True


async def upsert_chip_data(stock_code: str, trade_date: str) -> bool:
    """
    從 FinMind 抓取當日三大法人 + 融資融券，寫入 stock_indicators。
    需在 run_analysis 後執行（確保 indicators 行已存在）。
    """
    inst   = await fetch_institutional(stock_code, trade_date)
    margin = await fetch_margin(stock_code, trade_date)

    if not inst and not margin:
        logger.info(f"FinMind 無籌碼數據: {stock_code} {trade_date}")
        return False

    # 確保 indicators 行存在（若 analysis 還沒跑，先插入空行）
    await execute("""
        INSERT INTO stock_indicators (stock_code, trade_date)
        VALUES ($1, $2)
        ON CONFLICT (stock_code, trade_date) DO NOTHING
    """, stock_code, trade_date)

    if inst:
        await execute("""
            UPDATE stock_indicators SET
                foreign_net_buy          = $3,
                investment_trust_net_buy = $4,
                dealer_net_buy           = $5,
                institution_flow = CASE
                    WHEN $3 < 0 AND $4 < 0 THEN 'DOUBLE_SELL'::institution_flow_type
                    WHEN $3 < 0 OR  $4 < 0 THEN 'SINGLE_SELL'::institution_flow_type
                    ELSE 'HOLD_OR_BUY'::institution_flow_type
                END,
                updated_at = NOW()
            WHERE stock_code = $1 AND trade_date = $2
        """, stock_code, trade_date,
             inst["foreign_net_buy"], inst["investment_trust_net_buy"], inst["dealer_net_buy"])

    if margin:
        await execute("""
            UPDATE stock_indicators SET
                margin_balance        = $3,
                margin_short_shares   = $4,
                short_to_margin_ratio = $5,
                updated_at            = NOW()
            WHERE stock_code = $1 AND trade_date = $2
        """, stock_code, trade_date,
             margin["margin_balance"], margin["margin_short_shares"], margin["short_to_margin_ratio"])

    return True

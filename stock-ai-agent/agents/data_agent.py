"""
data_agent — 日K報價 + FinMind 籌碼數據 抓取與寫入
TWSE（上市）用官方 API；OTC（上櫃）改用 Yahoo Finance（TPEx 舊 API 已下架）。
"""
import httpx
import logging
import datetime
from datetime import date as _date
from typing import Optional

from utils.db import execute, fetch_all
from utils.finmind_client import (
    fetch_institutional, fetch_margin,
    fetch_shareholding, fetch_consecutive_foreign_days,
)

logger = logging.getLogger(__name__)

_TWSE_URL = (
    "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
    "?response=json&date={date}&stockNo={code}"
)
_YAHOO_URL      = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=10d"
_YAHOO_HIST_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range={days}d"
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StockBot/1.0)"}


def _parse_twse(data: dict) -> Optional[dict]:
    if data.get("stat") != "OK" or not data.get("data"):
        return None
    last = data["data"][-1]
    try:
        # columns: 0=date,1=volume(shares),2=amount,3=open,4=high,5=low,6=close,7=change(abs),8=transactions
        close = float(last[6].replace(",", ""))
        abs_change_str = last[7].replace(",", "").replace("+", "").replace("X", "").strip()
        abs_change = float(abs_change_str) if abs_change_str else 0.0
        prev_close = close - abs_change
        change_pct = round(abs_change / prev_close * 100, 2) if prev_close != 0 else 0.0
        return {
            "open_price":  float(last[3].replace(",", "")),
            "high_price":  float(last[4].replace(",", "")),
            "low_price":   float(last[5].replace(",", "")),
            "close_price": close,
            "volume":      int(last[1].replace(",", "")) // 1000,
            "change_pct":  max(-99.99, min(99.99, change_pct)),
        }
    except (ValueError, IndexError) as e:
        logger.warning(f"TWSE 解析失敗: {e}")
        return None


def _parse_yahoo(data: dict, target_date: str) -> Optional[dict]:
    """從 Yahoo Finance v8 chart API 取指定日期的 OHLCV。"""
    try:
        result = data["chart"]["result"]
        if not result:
            return None
        r = result[0]
        timestamps = r["timestamp"]
        quote = r["indicators"]["quote"][0]
        opens = quote["open"]
        highs = quote["high"]
        lows = quote["low"]
        closes = quote["close"]
        volumes = quote["volume"]

        target = _date.fromisoformat(target_date)
        for i, ts in enumerate(timestamps):
            day = datetime.date.fromtimestamp(ts)
            if day == target:
                o = opens[i] or 0
                h = highs[i] or 0
                l = lows[i] or 0
                c = closes[i] or 0
                v = volumes[i] or 0
                prev = closes[i - 1] if i > 0 and closes[i - 1] else c
                change_pct = round((c - prev) / prev * 100, 2) if prev else 0.0
                return {
                    "open_price":  round(o, 2),
                    "high_price":  round(h, 2),
                    "low_price":   round(l, 2),
                    "close_price": round(c, 2),
                    "volume":      int(v) // 1000,
                    "change_pct":  max(-99.99, min(99.99, change_pct)),
                }
        logger.info(f"Yahoo Finance 無 {target_date} 資料（可能休市）")
        return None
    except (KeyError, IndexError, TypeError) as e:
        logger.warning(f"Yahoo Finance 解析失敗: {e}")
        return None


async def _yahoo_get(client: httpx.AsyncClient, stock_code: str) -> Optional[dict]:
    """嘗試 .TW 後綴，若無資料改用 .TWO（部分上櫃股票格式不同）。"""
    for suffix in (".TW", ".TWO"):
        ticker = f"{stock_code}{suffix}"
        try:
            r = await client.get(_YAHOO_URL.format(ticker=ticker))
            data = r.json()
            if data.get("chart", {}).get("result"):
                return data
        except Exception:
            pass
    return None


async def fetch_price(stock_code: str, trade_date: str, market: str = "TWSE") -> Optional[dict]:
    date_fmt = trade_date.replace("-", "")
    try:
        async with httpx.AsyncClient(timeout=15, headers=_YAHOO_HEADERS) as c:
            if market == "OTC":
                data = await _yahoo_get(c, stock_code)
                return _parse_yahoo(data, trade_date) if data else None
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

    td = _date.fromisoformat(trade_date)
    await execute("""
        INSERT INTO stock_prices
            (stock_code, trade_date, open_price, high_price, low_price, close_price, volume, change_pct)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        ON CONFLICT (stock_code, trade_date) DO UPDATE SET
            close_price = EXCLUDED.close_price,
            volume      = EXCLUDED.volume,
            change_pct  = EXCLUDED.change_pct
    """, stock_code, td,
         data["open_price"], data["high_price"], data["low_price"],
         data["close_price"], data["volume"], data["change_pct"])
    return True


async def backfill_prices(stock_code: str, days: int = 90) -> int:
    """
    用 Yahoo Finance 一次補抓 N 天歷史 K 線，寫入 stock_prices。
    自動嘗試 .TW / .TWO 後綴。回傳成功寫入的天數。
    """
    result = None
    async with httpx.AsyncClient(timeout=20, headers=_YAHOO_HEADERS) as c:
        for suffix in (".TW", ".TWO"):
            ticker = f"{stock_code}{suffix}"
            url = _YAHOO_HIST_URL.format(ticker=ticker, days=days)
            try:
                r = await c.get(url)
                result = r.json().get("chart", {}).get("result")
                if result:
                    break
            except Exception:
                pass
    try:
        if not result:
            logger.warning(f"Yahoo 無歷史資料: {stock_code}")
            return 0
        res = result[0]
        timestamps = res["timestamp"]
        q = res["indicators"]["quote"][0]
        opens = q["open"]; highs = q["high"]; lows = q["low"]
        closes = q["close"]; volumes = q["volume"]

        written = 0
        for i, ts in enumerate(timestamps):
            c_val = closes[i]
            if not c_val:
                continue
            day = datetime.date.fromtimestamp(ts)
            prev = closes[i - 1] if i > 0 and closes[i - 1] else c_val
            change_pct = round((c_val - prev) / prev * 100, 2) if prev else 0.0
            await execute("""
                INSERT INTO stock_prices
                    (stock_code, trade_date, open_price, high_price, low_price, close_price, volume, change_pct)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (stock_code, trade_date) DO UPDATE SET
                    close_price = EXCLUDED.close_price,
                    volume      = EXCLUDED.volume,
                    change_pct  = EXCLUDED.change_pct
            """, stock_code, day,
                 round(opens[i] or 0, 2), round(highs[i] or 0, 2),
                 round(lows[i] or 0, 2), round(c_val, 2),
                 int(volumes[i] or 0) // 1000,
                 max(-99.99, min(99.99, change_pct)))
            written += 1
        return written
    except Exception as e:
        logger.warning(f"回填失敗 {stock_code}: {e}")
        return 0


async def _calc_short_cover_days(stock_code: str, trade_date: str, short_shares: int) -> float | None:
    """融券回補預估天數 = 融券餘額 / 近5日均量。"""
    if short_shares <= 0:
        return None
    td = _date.fromisoformat(trade_date)
    rows = await fetch_all("""
        SELECT volume FROM stock_prices
        WHERE stock_code = $1 AND trade_date <= $2
        ORDER BY trade_date DESC LIMIT 5
    """, stock_code, td)
    if not rows:
        return None
    avg_vol = sum(float(r["volume"] or 0) for r in rows) / len(rows)
    if avg_vol <= 0:
        return None
    return round(short_shares / avg_vol, 1)


async def _calc_margin_trend_5d(stock_code: str, trade_date: str) -> float | None:
    """5日融資餘額變化百分比 = (今日 - 5日前) / 5日前 * 100。"""
    td = _date.fromisoformat(trade_date)
    rows = await fetch_all("""
        SELECT margin_balance FROM stock_indicators
        WHERE stock_code = $1 AND trade_date <= $2 AND margin_balance IS NOT NULL
        ORDER BY trade_date DESC LIMIT 6
    """, stock_code, td)
    if len(rows) < 2:
        return None
    latest = float(rows[0]["margin_balance"])
    prev   = float(rows[-1]["margin_balance"])
    if prev == 0:
        return None
    return round((latest - prev) / prev * 100, 2)


async def upsert_chip_data(stock_code: str, trade_date: str) -> bool:
    """
    從 FinMind 抓取三大法人、融資融券、外資持股比例，寫入 stock_indicators。
    需在 run_analysis 後執行（確保 indicators 行已存在）。
    """
    inst         = await fetch_institutional(stock_code, trade_date)
    margin       = await fetch_margin(stock_code, trade_date)
    shareholding = await fetch_shareholding(stock_code, trade_date)
    consec_days  = await fetch_consecutive_foreign_days(stock_code, trade_date)

    if not inst and not margin and not shareholding:
        logger.info(f"FinMind 無籌碼數據: {stock_code} {trade_date}")
        return False

    td = _date.fromisoformat(trade_date)
    await execute("""
        INSERT INTO stock_indicators (stock_code, trade_date)
        VALUES ($1, $2)
        ON CONFLICT (stock_code, trade_date) DO NOTHING
    """, stock_code, td)

    if inst:
        await execute("""
            UPDATE stock_indicators SET
                foreign_net_buy          = $3,
                investment_trust_net_buy = $4,
                dealer_net_buy           = $5,
                foreign_consecutive_days = $6,
                institution_flow = CASE
                    WHEN $3 < 0 AND $4 < 0 THEN 'DOUBLE_SELL'::institution_flow_type
                    WHEN $3 < 0 OR  $4 < 0 THEN 'SINGLE_SELL'::institution_flow_type
                    ELSE 'HOLD_OR_BUY'::institution_flow_type
                END,
                updated_at = NOW()
            WHERE stock_code = $1 AND trade_date = $2
        """, stock_code, td,
             inst["foreign_net_buy"], inst["investment_trust_net_buy"],
             inst["dealer_net_buy"], consec_days)

    if margin:
        short_shares = margin["margin_short_shares"]
        short_cover  = await _calc_short_cover_days(stock_code, trade_date, short_shares)
        margin_trend = await _calc_margin_trend_5d(stock_code, trade_date)
        await execute("""
            UPDATE stock_indicators SET
                margin_balance        = $3,
                margin_short_shares   = $4,
                short_to_margin_ratio = $5,
                short_cover_days      = $6,
                margin_trend_5d       = $7,
                updated_at            = NOW()
            WHERE stock_code = $1 AND trade_date = $2
        """, stock_code, td,
             margin["margin_balance"], short_shares,
             margin["short_to_margin_ratio"], short_cover, margin_trend)

    if shareholding and shareholding.get("foreign_holding_ratio") is not None:
        await execute("""
            UPDATE stock_indicators SET
                foreign_holding_ratio = $3,
                updated_at            = NOW()
            WHERE stock_code = $1 AND trade_date = $2
        """, stock_code, td, shareholding["foreign_holding_ratio"])

    return True

"""
股價資料抓取（Yahoo Finance + TWSE 官方 API）
支援上市（TWSE）與上櫃（OTC）股票
"""
import httpx
import logging
import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StockScreener/2.0)"}

# Yahoo Finance：近 10 日日 K
_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=15d"
# TWSE 月成交資料（上市）
_TWSE_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={date}&stockNo={code}"


async def _yahoo_ohlcv(stock_code: str) -> list[dict]:
    """
    嘗試 .TW（上市）再嘗試 .TWO（上櫃），回傳近 15 個交易日 OHLCV 清單。
    每筆：{date, open, high, low, close, volume}
    """
    async with httpx.AsyncClient(timeout=15, headers=_YAHOO_HEADERS) as c:
        for suffix in (".TW", ".TWO"):
            ticker = f"{stock_code}{suffix}"
            try:
                r = await c.get(_YAHOO_CHART.format(ticker=ticker))
                data = r.json()
                result = data.get("chart", {}).get("result")
                if not result:
                    continue
                res = result[0]
                ts_list = res.get("timestamp", [])
                q = res["indicators"]["quote"][0]
                rows = []
                for i, ts in enumerate(ts_list):
                    c_val = q["close"][i]
                    if c_val is None:
                        continue
                    rows.append({
                        "date":   datetime.date.fromtimestamp(ts),
                        "open":   round(q["open"][i] or 0, 2),
                        "high":   round(q["high"][i] or 0, 2),
                        "low":    round(q["low"][i] or 0, 2),
                        "close":  round(c_val, 2),
                        "volume": int(q["volume"][i] or 0) // 1000,  # 張
                    })
                if rows:
                    return rows
            except Exception as e:
                logger.debug(f"Yahoo {ticker}: {e}")
    return []


async def fetch_stock_data(stock_code: str) -> Optional[dict]:
    """
    回傳篩選所需的所有欄位：
    {
      close, prev_close, daily_return,
      volume_today, avg_volume_5d, volume_ratio,
      turnover_rate,     # 需要流通股數，若無則為 None
      closes[-6:]        # 近 6 日收盤（計算 MA）
      volumes[-3:]       # 近 3 日成交量（量能形態）
      ma5, ma10, ma20, ma60
    }
    """
    rows = await _yahoo_ohlcv(stock_code)
    if len(rows) < 7:
        logger.debug(f"{stock_code}: 歷史資料不足（{len(rows)} 筆）")
        return None

    rows.sort(key=lambda x: x["date"])

    closes  = [r["close"]  for r in rows]
    volumes = [r["volume"] for r in rows]

    close_today = closes[-1]
    prev_close  = closes[-2]
    if prev_close == 0:
        return None

    daily_return  = (close_today - prev_close) / prev_close
    volume_today  = volumes[-1]
    avg_vol_5d    = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else None

    def _sma(lst, n):
        if len(lst) < n:
            return None
        return sum(lst[-n:]) / n

    ma5  = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60) if len(closes) >= 60 else None

    # MA 斜率（近 3 日均線方向）
    def _slope(lst, n):
        if len(lst) < n + 3:
            return None
        old = sum(lst[-n-3:-3]) / n
        new = sum(lst[-n:]) / n
        if old == 0:
            return None
        return (new - old) / old

    return {
        "close":         close_today,
        "prev_close":    prev_close,
        "daily_return":  daily_return,
        "volume_today":  volume_today,
        "avg_volume_5d": avg_vol_5d,
        "volume_ratio":  volume_today / avg_vol_5d if avg_vol_5d else None,
        "volumes_3d":    volumes[-3:],   # [t-2, t-1, t]
        "closes":        closes,
        "ma5":           ma5,
        "ma10":          ma10,
        "ma20":          ma20,
        "ma60":          ma60,
        "ma5_slope":     _slope(closes, 5),
        "ma10_slope":    _slope(closes, 10),
        "ma20_slope":    _slope(closes, 20),
    }


async def fetch_market_return(trade_date: datetime.date) -> float:
    """
    抓取大盤（加權指數 ^TWII）當日漲幅。
    失敗時回傳 0.0。
    """
    try:
        async with httpx.AsyncClient(timeout=10, headers=_YAHOO_HEADERS) as c:
            r = await c.get(_YAHOO_CHART.format(ticker="^TWII"))
            data = r.json()
            result = data.get("chart", {}).get("result")
            if not result:
                return 0.0
            ts_list = result[0].get("timestamp", [])
            closes  = result[0]["indicators"]["quote"][0]["close"]
            for i, ts in enumerate(ts_list):
                if datetime.date.fromtimestamp(ts) == trade_date and i > 0:
                    prev = closes[i - 1]
                    curr = closes[i]
                    if prev and curr:
                        return (curr - prev) / prev
    except Exception as e:
        logger.warning(f"大盤漲幅抓取失敗: {e}")
    return 0.0

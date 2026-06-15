"""
富果 (Fugle) 即時報價
用於取得追蹤股當日正確報價、漲跌幅、成交量
"""
import os
import httpx
import logging
import datetime
import asyncio

logger = logging.getLogger(__name__)

# 富果 Market Data API
_FUGLE_KEY = os.environ.get("FUGLE_API_KEY", "")
_FUGLE_BASE = "https://api.fugle.tw/marketdata/v1.0/stock/intraday"

# Fallback: Yahoo Finance（已有）
_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StockScreener/2.0)"}


async def fetch_quote(stock_code: str) -> dict | None:
    """
    取得個股當日報價。
    優先使用富果 API，失敗時 fallback 至 Yahoo Finance。
    回傳：{code, name, close, prev_close, change, change_pct,
            volume, open, high, low, time}
    """
    # 嘗試富果
    if _FUGLE_KEY:
        result = await _fugle_quote(stock_code)
        if result:
            return result

    # Fallback: Yahoo Finance
    return await _yahoo_quote(stock_code)


async def _fugle_quote(stock_code: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{_FUGLE_BASE}/quote/{stock_code}",
                headers={"X-API-KEY": _FUGLE_KEY},
            )
            if r.status_code != 200:
                return None
            d = r.json()
            close      = d.get("closePrice") or d.get("lastPrice") or 0
            prev_close = d.get("referencePrice") or d.get("previousClose") or 0
            change     = round(close - prev_close, 2) if prev_close else 0
            change_pct = round(change / prev_close * 100, 2) if prev_close else 0
            return {
                "code":       stock_code,
                "name":       d.get("name", ""),
                "close":      round(float(close), 2),
                "prev_close": round(float(prev_close), 2),
                "change":     change,
                "change_pct": change_pct,
                "volume":     d.get("volumeAtClose") or d.get("volume") or 0,
                "open":       d.get("openPrice") or 0,
                "high":       d.get("highPrice") or 0,
                "low":        d.get("lowPrice") or 0,
                "time":       d.get("lastUpdated", ""),
                "source":     "fugle",
            }
    except Exception as e:
        logger.debug(f"Fugle quote {stock_code}: {e}")
        return None


async def _yahoo_quote(stock_code: str) -> dict | None:
    for suffix in (".TW", ".TWO"):
        try:
            async with httpx.AsyncClient(timeout=10, headers=_YAHOO_HEADERS) as c:
                ticker = f"{stock_code}{suffix}"
                r = await c.get(_YAHOO_CHART.format(ticker=ticker))
                data = r.json()
                result = data.get("chart", {}).get("result")
                if not result:
                    continue
                res    = result[0]
                meta   = res.get("meta", {})
                q      = res["indicators"]["quote"][0]
                ts_list = res.get("timestamp", [])
                if not ts_list:
                    continue

                tz8 = datetime.timezone(datetime.timedelta(hours=8))
                closes  = q.get("close",  [])
                volumes = q.get("volume", [])
                opens   = q.get("open",   [])
                highs   = q.get("high",   [])
                lows    = q.get("low",    [])

                # 取最後一個有效收盤
                close = prev_close = None
                vol = open_ = high = low = 0
                for i in range(len(ts_list) - 1, -1, -1):
                    if closes[i] is not None and close is None:
                        close = closes[i]
                        vol   = int(volumes[i] or 0) // 1000
                        open_ = opens[i] or 0
                        high  = highs[i] or 0
                        low   = lows[i] or 0
                    elif closes[i] is not None and close is not None:
                        prev_close = closes[i]
                        break

                if close is None:
                    continue

                prev_close = prev_close or meta.get("previousClose") or meta.get("chartPreviousClose") or 0
                change     = round(close - prev_close, 2) if prev_close else 0
                change_pct = round(change / prev_close * 100, 2) if prev_close else 0

                return {
                    "code":       stock_code,
                    "name":       meta.get("shortName", ""),
                    "close":      round(float(close), 2),
                    "prev_close": round(float(prev_close), 2),
                    "change":     change,
                    "change_pct": change_pct,
                    "volume":     vol,
                    "open":       round(float(open_), 2),
                    "high":       round(float(high), 2),
                    "low":        round(float(low), 2),
                    "time":       "",
                    "source":     "yahoo",
                }
        except Exception as e:
            logger.debug(f"Yahoo quote {stock_code}{suffix}: {e}")
    return None


async def fetch_all_quotes(stock_codes: list[str], max_concurrent: int = 10) -> dict[str, dict]:
    """批量取得多檔股票報價。回傳 {stock_code: quote_dict}"""
    sem = asyncio.Semaphore(max_concurrent)

    async def _one(code):
        async with sem:
            return code, await fetch_quote(code)

    results = await asyncio.gather(*[_one(c) for c in stock_codes])
    return {code: q for code, q in results if q is not None}

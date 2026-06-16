"""
股價與成交量資料（Yahoo Finance）
支援上市 .TW / 上櫃 .TWO
"""
import httpx
import logging
import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_HEADERS   = {"User-Agent": "Mozilla/5.0 (compatible; ICScreener/1.0)"}
_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=90d"
_TZ8       = datetime.timezone(datetime.timedelta(hours=8))


async def fetch_ohlcv(stock_code: str) -> list[dict]:
    """
    抓取近 90 日 OHLCV，自動嘗試 .TW / .TWO。
    回傳：[{date, open, high, low, close, volume}]，按日期升序。
    """
    async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as c:
        for suffix in (".TW", ".TWO"):
            ticker = f"{stock_code}{suffix}"
            try:
                r    = await c.get(_YAHOO_URL.format(ticker=ticker))
                data = r.json()
                res  = data.get("chart", {}).get("result")
                if not res:
                    continue
                res    = res[0]
                ts_lst = res.get("timestamp", [])
                q      = res["indicators"]["quote"][0]
                rows   = []
                for i, ts in enumerate(ts_lst):
                    c_val = q["close"][i]
                    if c_val is None:
                        continue
                    rows.append({
                        "date":   datetime.datetime.fromtimestamp(ts, tz=_TZ8).date(),
                        "open":   round(q["open"][i]   or 0, 2),
                        "high":   round(q["high"][i]   or 0, 2),
                        "low":    round(q["low"][i]    or 0, 2),
                        "close":  round(c_val, 2),
                        "volume": int(q["volume"][i]   or 0) // 1000,  # 股 → 張
                    })
                if rows:
                    rows.sort(key=lambda x: x["date"])
                    return rows
            except Exception as e:
                logger.debug(f"Yahoo {ticker}: {e}")
    return []


async def fetch_price_metrics(stock_code: str) -> Optional[dict]:
    """
    計算篩選所需的價格指標：
    - 近3/5交易日平均量
    - 近1週/1月/1季漲幅
    - 52週最高價 & 距高點跌幅
    - 當日收盤、前日收盤
    """
    rows = await fetch_ohlcv(stock_code)
    if len(rows) < 20:
        return None

    closes  = [r["close"]  for r in rows]
    volumes = [r["volume"] for r in rows]
    today_c = closes[-1]

    def pct(old: float) -> Optional[float]:
        return round((today_c - old) / old * 100, 2) if old else None

    # 近 N 交易日均量
    avg_vol_3d = sum(volumes[-3:]) / 3
    avg_vol_5d = sum(volumes[-5:]) / 5

    # 漲幅（交易日數近似：1週≈5日、1月≈22日、1季≈65日）
    w1_pct  = pct(closes[-6])  if len(closes) >= 6  else None
    m1_pct  = pct(closes[-23]) if len(closes) >= 23 else None
    q1_pct  = pct(closes[-66]) if len(closes) >= 66 else None

    # 52週高點（取最近 250 筆，但 90d 資料只有 ~62 筆，用全部）
    high_52w    = max(r["high"] for r in rows)
    dist_high   = round((today_c - high_52w) / high_52w * 100, 2)  # 負數 = 距高點跌幅

    return {
        "close":       today_c,
        "prev_close":  closes[-2],
        "volume_today": volumes[-1],
        "avg_vol_3d":  round(avg_vol_3d, 0),
        "avg_vol_5d":  round(avg_vol_5d, 0),
        "w1_pct":      w1_pct,    # 近1週漲幅
        "m1_pct":      m1_pct,    # 近1月漲幅
        "q1_pct":      q1_pct,    # 近1季漲幅
        "high_52w":    high_52w,
        "dist_high_pct": dist_high,  # 距52週高點（負=跌幅）
    }

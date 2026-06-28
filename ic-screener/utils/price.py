"""
股價與成交量資料（FinMind TaiwanStockPrice）
避免 Yahoo Finance 在雲端 IP 被封鎖的問題。
"""
import os
import httpx
import logging
import datetime
from typing import Optional

logger = logging.getLogger(__name__)

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN") or os.environ.get("FINMIND_API_KEY", "")
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"


async def fetch_ohlcv(stock_code: str, days: int = 90) -> list[dict]:
    """
    抓取近 days 日 OHLCV（FinMind TaiwanStockPrice）。
    回傳：[{date, open, high, low, close, volume(張)}]，按日期升序。
    """
    end   = datetime.date.today()
    # 多抓一點以涵蓋非交易日，目標取約 days 個交易日
    start = end - datetime.timedelta(days=int(days * 1.6))
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(FINMIND_BASE, params={
                "dataset":    "TaiwanStockPrice",
                "data_id":    stock_code,
                "start_date": str(start),
                "end_date":   str(end),
                "token":      FINMIND_TOKEN,
            })
            data = r.json()
            if data.get("status") != 200:
                logger.warning(f"TaiwanStockPrice {stock_code}: {data.get('msg')}")
                return []
            rows = data.get("data", [])
    except Exception as e:
        logger.warning(f"TaiwanStockPrice {stock_code}: {e}")
        return []

    result = []
    for r in rows:
        try:
            result.append({
                "date":   datetime.date.fromisoformat(r["date"]),
                "open":   float(r.get("open") or 0),
                "high":   float(r.get("max")  or 0),
                "low":    float(r.get("min")  or 0),
                "close":  float(r.get("close") or 0),
                "volume": int(r.get("Trading_Volume", 0) or 0) // 1000,  # 股 → 張
            })
        except (KeyError, ValueError):
            continue
    result.sort(key=lambda x: x["date"])
    return result[-days:]  # 取最後 days 個交易日


async def fetch_price_metrics(stock_code: str) -> Optional[dict]:
    """
    計算篩選所需的價格指標：
    - 近3/5交易日平均量
    - 近1週/1月/1季漲幅
    - 最高價 & 距高點跌幅
    - 當日收盤、前日收盤
    """
    rows = await fetch_ohlcv(stock_code, days=90)
    if len(rows) < 5:
        logger.warning(f"價格資料不足 {stock_code}: only {len(rows)} rows")
        return None

    closes  = [r["close"]  for r in rows]
    volumes = [r["volume"] for r in rows]
    today_c = closes[-1]

    def pct(old: float) -> Optional[float]:
        return round((today_c - old) / old * 100, 2) if old else None

    avg_vol_3d = sum(volumes[-3:]) / min(3, len(volumes))
    avg_vol_5d = sum(volumes[-5:]) / min(5, len(volumes))

    # 漲幅（交易日數近似：1週≈5日、1月≈22日、1季≈65日）
    w1_pct  = pct(closes[-6])  if len(closes) >= 6  else None
    m1_pct  = pct(closes[-23]) if len(closes) >= 23 else None
    q1_pct  = pct(closes[-66]) if len(closes) >= 66 else None

    high_52w  = max(r["high"] for r in rows)
    dist_high = round((today_c - high_52w) / high_52w * 100, 2) if high_52w else 0

    return {
        "close":         today_c,
        "prev_close":    closes[-2] if len(closes) >= 2 else today_c,
        "volume_today":  volumes[-1],
        "avg_vol_3d":    round(avg_vol_3d, 0),
        "avg_vol_5d":    round(avg_vol_5d, 0),
        "w1_pct":        w1_pct,
        "m1_pct":        m1_pct,
        "q1_pct":        q1_pct,
        "high_52w":      high_52w,
        "dist_high_pct": dist_high,
    }

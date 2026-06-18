"""
FinMind 市場資料抓取
- 台指期三大法人倉位（TX）
- USD/TWD 匯率
- 個股融資融券餘額（聚合）
"""
import os
import asyncio
import httpx
import logging
import datetime

logger = logging.getLogger(__name__)

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"


async def _get(dataset: str, data_id: str = None,
               days: int = 30) -> list[dict]:
    end   = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    params = {"dataset": dataset, "start_date": str(start),
              "end_date": str(end), "token": FINMIND_TOKEN}
    if data_id:
        params["data_id"] = data_id
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(FINMIND_BASE, params=params)
            d = r.json()
            if d.get("status") != 200:
                logger.warning(f"FinMind {dataset}: {d.get('msg')}")
                return []
            return d.get("data", [])
    except Exception as e:
        logger.warning(f"FinMind {dataset} error: {e}")
        return []


async def fetch_futures_institutional() -> dict:
    """
    台指期三大法人未平倉（TX）。
    回傳：外資淨空單口數、多空比、近5日變化趨勢。
    """
    rows = await _get("TaiwanFuturesInstitutionalInvestors", "TX", days=30)
    if not rows:
        return {}

    # 取外資資料，依日期排序
    foreign = sorted([r for r in rows if r.get("institutional_investors") == "外資"],
                     key=lambda x: x["date"])
    if not foreign:
        return {}

    latest = foreign[-1]
    long_oi  = latest["long_open_interest_balance_volume"]
    short_oi = latest["short_open_interest_balance_volume"]
    net_short = short_oi - long_oi  # 正值 = 淨空

    # 近5筆淨空變化（趨勢）
    recent5 = foreign[-5:]
    net_shorts_5d = [r["short_open_interest_balance_volume"] -
                     r["long_open_interest_balance_volume"] for r in recent5]
    trend_5d = net_shorts_5d[-1] - net_shorts_5d[0] if len(net_shorts_5d) >= 2 else 0

    return {
        "date":         latest["date"],
        "long_oi":      long_oi,
        "short_oi":     short_oi,
        "net_short":    net_short,       # 正=淨空頭
        "trend_5d":     trend_5d,        # 正=淨空持續增加（空頭加碼）
        "net_shorts_5d": net_shorts_5d,
    }


async def fetch_usdtwd() -> dict:
    """
    USD/TWD 匯率，計算月變化幅度。
    台幣貶值（USD上升）= 外資匯出信號。
    """
    rows = await _get("TaiwanExchangeRate", "USD", days=35)
    usd_rows = sorted([r for r in rows if r.get("currency") == "USD"],
                      key=lambda x: x["date"])
    if not usd_rows:
        return {}

    latest_rate = usd_rows[-1]["spot_buy"]
    base_rate   = usd_rows[0]["spot_buy"]   # ~1個月前
    deprec_pct  = round((latest_rate - base_rate) / base_rate * 100, 3) if base_rate else 0

    return {
        "date":         usd_rows[-1]["date"],
        "rate":         latest_rate,
        "base_rate":    base_rate,
        "deprec_pct_1m": deprec_pct,    # 正=台幣貶值
    }


async def fetch_margin_aggregate(stock_codes: list[str]) -> dict:
    """
    聚合追蹤股的融資/融券餘額，計算整體籌碼鬆緊。
    """
    tasks = [_get("TaiwanStockMarginPurchaseShortSale", code, days=35)
             for code in stock_codes[:20]]  # 限制20檔避免 rate limit
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_margin_today = 0
    total_margin_30d   = 0
    total_short_today  = 0
    count = 0

    for rows in results:
        if isinstance(rows, Exception) or not rows:
            continue
        sorted_rows = sorted(rows, key=lambda x: x["date"])
        if len(sorted_rows) >= 2:
            total_margin_today += sorted_rows[-1].get("MarginPurchaseTodayBalance", 0) or 0
            total_margin_30d   += sorted_rows[0].get("MarginPurchaseTodayBalance", 0) or 0
            total_short_today  += sorted_rows[-1].get("ShortSaleTodayBalance", 0) or 0
            count += 1

    if count == 0:
        return {}

    margin_chg_pct = round((total_margin_today - total_margin_30d) /
                           total_margin_30d * 100, 2) if total_margin_30d else 0

    return {
        "stocks_sampled":   count,
        "margin_today":     total_margin_today,
        "margin_30d_ago":   total_margin_30d,
        "margin_chg_pct":   margin_chg_pct,    # 負=融資減少（籌碼鬆動）
        "short_balance":    total_short_today,
    }

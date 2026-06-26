"""
三大法人籌碼資料（外資、投信、自營商）
使用 FinMind API
"""
import os
import httpx
import logging
import datetime
from typing import Optional

logger = logging.getLogger(__name__)

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"


async def _finmind_get(dataset: str, stock_code: str,
                       start_date: datetime.date, end_date: datetime.date) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(FINMIND_BASE, params={
                "dataset":    dataset,
                "data_id":    stock_code,
                "start_date": str(start_date),
                "end_date":   str(end_date),
                "token":      FINMIND_TOKEN,
            })
            data = r.json()
            if data.get("status") != 200:
                logger.warning(f"FinMind {dataset} {stock_code}: {data.get('msg')}")
                return []
            return data.get("data", [])
    except Exception as e:
        logger.warning(f"FinMind request failed ({dataset} {stock_code}): {e}")
        return []


async def fetch_institutional_flows(stock_code: str, trade_date: datetime.date) -> dict:
    """
    取得指定股票當日三大法人買賣超（張數）。
    回傳：{foreign_net, trust_net, dealer_net, total_net,
           foreign_buy, foreign_sell, foreign_consec}
    """
    start = trade_date - datetime.timedelta(days=14)
    records = await _finmind_get(
        "TaiwanStockInstitutionalInvestorsBuySell",
        stock_code, start, trade_date
    )
    # 若今日無資料（市場仍開盤中），自動 fallback 至最近一個有資料的日期
    if records:
        latest_date = max(r.get("date", "") for r in records)
        if latest_date != str(trade_date):
            trade_date = datetime.date.fromisoformat(latest_date)

    result = {
        "foreign_buy":   0,
        "foreign_sell":  0,
        "foreign_net":   0,
        "trust_net":     0,
        "dealer_net":    0,
        "total_net":     0,
        "foreign_consec": 0,
    }

    # FinMind 欄位名稱（英文）：
    # Foreign_Investor / Foreign_Dealer_Self → 外資
    # Investment_Trust                        → 投信
    # Dealer_self / Dealer_Hedging            → 自營商
    # 單位：股 → 除以 1000 換算為 張

    by_date: dict[str, dict] = {}
    for rec in records:
        d = rec.get("date", "")
        if d not in by_date:
            by_date[d] = {"foreign": 0, "trust": 0, "dealer": 0}
        name = rec.get("name", "")
        buy  = int(rec.get("buy",  0) or 0) // 1000   # 股 → 張
        sell = int(rec.get("sell", 0) or 0) // 1000
        net  = buy - sell
        if name in ("Foreign_Investor", "Foreign_Dealer_Self",
                    "外資", "外資自營", "外資及陸資(不含外資自營商)", "外資自營商"):
            by_date[d]["foreign"] += net
            if d == str(trade_date) and name in ("Foreign_Investor", "外資", "外資及陸資(不含外資自營商)"):
                result["foreign_buy"]  = buy
                result["foreign_sell"] = sell
        elif name in ("Investment_Trust", "投信"):
            by_date[d]["trust"] += net
        elif name in ("Dealer_self", "Dealer_Hedging", "自營商", "自營商(自行買賣)", "自營商(避險)"):
            by_date[d]["dealer"] += net

    # 今日合計
    today_str = str(trade_date)
    if today_str in by_date:
        td = by_date[today_str]
        result["foreign_net"] = td["foreign"]
        result["trust_net"]   = td["trust"]
        result["dealer_net"]  = td["dealer"]
        result["total_net"]   = td["foreign"] + td["trust"] + td["dealer"]

    # 計算外資連續買超天數（正=連買，負=連賣）
    sorted_dates = sorted(by_date.keys(), reverse=True)
    consec = 0
    for d in sorted_dates:
        net = by_date[d]["foreign"]
        if consec == 0:
            consec = 1 if net > 0 else (-1 if net < 0 else 0)
        elif consec > 0 and net > 0:
            consec += 1
        elif consec < 0 and net < 0:
            consec -= 1
        else:
            break
    result["foreign_consec"] = consec

    return result


async def fetch_foreign_shareholding(stock_code: str) -> dict:
    """
    取得外資持股比例（%）。
    使用 TaiwanStockShareholding dataset。
    """
    end   = datetime.date.today()
    start = end - datetime.timedelta(days=30)
    records = await _finmind_get(
        "TaiwanStockShareholding", stock_code, start, end
    )
    if not records:
        return {}

    latest = records[-1]
    total          = int(latest.get("NumberOfSharesIssued", 0) or 0)
    foreign_shares = int(latest.get("ForeignInvestmentShares", 0) or 0)
    ratio          = float(latest.get("ForeignInvestmentSharesRatio", 0) or 0)
    remain_ratio   = float(latest.get("ForeignInvestmentRemainRatio", 0) or 0)
    upper_limit    = float(latest.get("ForeignInvestmentUpperLimitRatio", 0) or 0)

    return {
        "date":              latest.get("date"),
        "foreign_ratio":     round(ratio, 2),          # 外資持股比例 %
        "foreign_remain_ratio": round(remain_ratio, 2), # 外資剩餘可買比例 %
        "foreign_upper_limit":  round(upper_limit, 2),  # 外資持股上限 %
        "foreign_shares":    foreign_shares,            # 外資持股（股）
        "total_shares":      total,                     # 發行股數（股）
    }


async def fetch_consecutive_foreign_days(stock_code: str,
                                         trade_date: datetime.date,
                                         lookback: int = 20) -> int:
    """回傳外資連續買超/賣超天數。正數=連買，負數=連賣。"""
    start   = trade_date - datetime.timedelta(days=lookback * 2)
    records = await _finmind_get(
        "TaiwanStockInstitutionalInvestorsBuySell",
        stock_code, start, trade_date
    )
    by_date: dict[str, int] = {}
    for rec in records:
        d    = rec.get("date", "")
        name = rec.get("name", "")
        if name in ("Foreign_Investor", "Foreign_Dealer_Self",
                    "外資", "外資自營", "外資及陸資(不含外資自營商)", "外資自營商"):
            buy  = int(rec.get("buy",  0) or 0) // 1000
            sell = int(rec.get("sell", 0) or 0) // 1000
            by_date[d] = by_date.get(d, 0) + (buy - sell)

    consec = 0
    for d in sorted(by_date.keys(), reverse=True):
        net = by_date[d]
        if consec == 0:
            consec = 1 if net > 0 else (-1 if net < 0 else 0)
        elif consec > 0 and net > 0:
            consec += 1
        elif consec < 0 and net < 0:
            consec -= 1
        else:
            break
    return consec


async def fetch_foreign_ratio_trend(stock_code: str, days: int = 5) -> dict:
    """
    取得外資持股比例近 days 日趨勢。
    回傳 {ratios, dates, rising_days, trend, latest}
    rising_days > 0 表示連續上升天數，< 0 表示連續下降天數。
    """
    end   = datetime.date.today()
    start = end - datetime.timedelta(days=days * 3)
    records = await _finmind_get("TaiwanStockShareholding", stock_code, start, end)
    if not records:
        return {"ratios": [], "dates": [], "rising_days": 0, "trend": "flat", "latest": 0}

    records = sorted(records, key=lambda x: x.get("date", ""))[-days:]
    ratios  = [float(r.get("ForeignInvestmentSharesRatio", 0) or 0) for r in records]
    dates   = [r.get("date", "") for r in records]

    consec = 0
    for i in range(len(ratios) - 1, 0, -1):
        diff = ratios[i] - ratios[i - 1]
        if consec == 0:
            consec = 1 if diff > 0 else (-1 if diff < 0 else 0)
        elif consec > 0 and diff > 0:
            consec += 1
        elif consec < 0 and diff < 0:
            consec -= 1
        else:
            break

    trend = "up" if consec > 0 else ("down" if consec < 0 else "flat")
    return {
        "ratios":      [round(r, 2) for r in ratios],
        "dates":       dates,
        "rising_days": consec,
        "trend":       trend,
        "latest":      ratios[-1] if ratios else 0,
    }

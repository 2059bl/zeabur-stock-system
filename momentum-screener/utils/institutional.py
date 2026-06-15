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

    result = {
        "foreign_buy":   0,
        "foreign_sell":  0,
        "foreign_net":   0,
        "trust_net":     0,
        "dealer_net":    0,
        "total_net":     0,
        "foreign_consec": 0,
    }

    # 按日期整理
    by_date: dict[str, dict] = {}
    for rec in records:
        d = rec.get("date", "")
        if d not in by_date:
            by_date[d] = {"foreign": 0, "trust": 0, "dealer": 0}
        name = rec.get("name", "")
        buy  = int(rec.get("buy",  0) or 0)
        sell = int(rec.get("sell", 0) or 0)
        net  = buy - sell
        if "外資" in name and "自行" not in name:
            by_date[d]["foreign"] += net
            if d == str(trade_date):
                result["foreign_buy"]  = buy
                result["foreign_sell"] = sell
        elif "投信" in name:
            by_date[d]["trust"] += net
        elif "自營" in name:
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
    total  = float(latest.get("TotalIssuedShares", 1) or 1)
    foreign_shares = float(latest.get("ForeignInvestmentShares", 0) or 0)
    ratio  = float(latest.get("ForeignInvestmentSharesRatio", 0) or 0)

    return {
        "date":          latest.get("date"),
        "foreign_ratio": round(ratio, 2),
        "foreign_shares": int(foreign_shares),
        "total_shares":   int(total),
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
        if "外資" in name and "自行" not in name:
            buy  = int(rec.get("buy",  0) or 0)
            sell = int(rec.get("sell", 0) or 0)
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

"""
FinMind API 客戶端
三大法人買賣超、融資融券、外資持股比例、台指期法人部位。
文件：https://finmindtrade.com/analysis/#/data/document
"""
import os
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

FINMIND_TOKEN = os.environ.get(
    "FINMIND_API_KEY",
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiTGkgbmluZyIsImVtYWlsIjoiMjA1OWJsQGdtYWlsLmNvbSIsInRva2VuX3ZlcnNpb24iOjB9.LqOpJ6__2UuEyGzuvBUtosDXW1kTJzIu2PtMjbamsRU",
)
_BASE = "https://api.finmindtrade.com/api/v4/data"


async def _fm_get(dataset: str, data_id: str, start_date: str) -> list[dict]:
    params = {
        "dataset": dataset,
        "data_id": data_id,
        "start_date": start_date,
        "token": FINMIND_TOKEN,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(_BASE, params=params)
            r.raise_for_status()
            data = r.json()
            if data.get("status") != 200:
                logger.warning(f"FinMind {dataset} {data_id}: {data.get('msg')}")
                return []
            return data.get("data", [])
    except Exception as e:
        logger.warning(f"FinMind API 失敗 {dataset} {data_id}: {e}")
        return []


async def fetch_institutional(stock_code: str, trade_date: str) -> Optional[dict]:
    """
    三大法人買賣超 (TaiwanStockInstitutionalInvestorsBuySell)
    回傳 {foreign_net_buy, investment_trust_net_buy, dealer_net_buy}
    """
    rows = await _fm_get("TaiwanStockInstitutionalInvestorsBuySell", stock_code, trade_date)
    result = {"foreign_net_buy": 0, "investment_trust_net_buy": 0, "dealer_net_buy": 0}
    name_map = {
        # FinMind v4 API 英文名稱（主要格式）
        "Foreign_Investor":    "foreign_net_buy",
        "Foreign_Dealer_Self": "foreign_net_buy",
        "Investment_Trust":    "investment_trust_net_buy",
        "Dealer_self":         "dealer_net_buy",
        "Dealer_Hedging":      "dealer_net_buy",
        # 中文名稱（部分環境或舊版 API）
        "外資":                "foreign_net_buy",
        "外資自營":             "foreign_net_buy",
        "外資及陸資(不含外資自營商)": "foreign_net_buy",
        "外資自營商":           "foreign_net_buy",
        "投信":                "investment_trust_net_buy",
        "自營商":               "dealer_net_buy",
        "自營商(自行買賣)":     "dealer_net_buy",
        "自營商(避險)":         "dealer_net_buy",
    }
    found = False
    for row in rows:
        if row.get("date") != trade_date:
            continue
        key = name_map.get(row.get("name", ""))
        if key:
            result[key] = result.get(key, 0) + int(row.get("buy") or 0) - int(row.get("sell") or 0)
            found = True
    return result if found else None


async def fetch_margin(stock_code: str, trade_date: str) -> Optional[dict]:
    """
    融資融券 (TaiwanStockMarginPurchaseShortSale)
    回傳 {margin_balance, margin_short_shares, short_to_margin_ratio}
    """
    rows = await _fm_get("TaiwanStockMarginPurchaseShortSale", stock_code, trade_date)
    for row in rows:
        if row.get("date") != trade_date:
            continue
        margin_balance = int(row.get("MarginPurchaseTodayBalance") or 0)
        short_shares   = int(row.get("ShortSaleTodayBalance") or 0)
        ratio = round(short_shares / margin_balance * 100, 2) if margin_balance > 0 else 0.0
        return {
            "margin_balance":       margin_balance,
            "margin_short_shares":  short_shares,
            "short_to_margin_ratio": ratio,
        }
    return None


async def fetch_shareholding(stock_code: str, trade_date: str) -> Optional[dict]:
    """
    外資持股比例 (TaiwanStockShareholding)
    回傳 {foreign_holding_ratio}  — 外資持股佔總股本 %
    """
    rows = await _fm_get("TaiwanStockShareholding", stock_code, trade_date)
    for row in rows:
        if row.get("date") != trade_date:
            continue
        total   = int(row.get("total_shares") or 0)
        foreign = int(row.get("ForeignInvestmentShares") or 0)
        ratio   = round(foreign / total * 100, 2) if total > 0 else None
        return {"foreign_holding_ratio": ratio}
    # TaiwanStockShareholding 發布頻率約每週，取最近一筆
    if rows:
        latest = max(rows, key=lambda r: r.get("date", ""))
        total   = int(latest.get("total_shares") or 0)
        foreign = int(latest.get("ForeignInvestmentShares") or 0)
        ratio   = round(foreign / total * 100, 2) if total > 0 else None
        return {"foreign_holding_ratio": ratio}
    return None


async def fetch_consecutive_foreign_days(stock_code: str, trade_date: str, lookback: int = 15) -> int:
    """
    計算外資連續買超(+)或連續賣超(-)天數。
    lookback: 往前查幾個交易日。
    """
    from datetime import date as _date, timedelta
    start = (_date.fromisoformat(trade_date) - timedelta(days=lookback * 2)).isoformat()
    rows = await _fm_get("TaiwanStockInstitutionalInvestorsBuySell", stock_code, start)

    # 匯總每日外資淨買超
    daily: dict[str, int] = {}
    for row in rows:
        if row.get("name") not in ("Foreign_Investor", "Foreign_Dealer_Self",
                                   "外資", "外資自營", "外資及陸資(不含外資自營商)", "外資自營商"):
            continue
        d = row.get("date", "")
        net = int(row.get("buy") or 0) - int(row.get("sell") or 0)
        daily[d] = daily.get(d, 0) + net

    sorted_days = sorted(daily.keys(), reverse=True)
    if not sorted_days:
        return 0

    # 從最新日往前計算連續方向
    first_sign = 1 if daily[sorted_days[0]] >= 0 else -1
    count = 0
    for d in sorted_days:
        sign = 1 if daily[d] >= 0 else -1
        if sign == first_sign:
            count += 1
        else:
            break
    return count * first_sign


async def fetch_futures_institutional(trade_date: str) -> Optional[dict]:
    """
    台指期三大法人淨部位 (TaiwanFuturesInstitutionalInvestors)
    回傳 {futures_foreign_net, futures_trust_net}
    """
    rows = await _fm_get("TaiwanFuturesInstitutionalInvestors", "TX", trade_date)
    result = {"futures_foreign_net": 0, "futures_trust_net": 0}
    found = False
    for row in rows:
        if row.get("date") != trade_date:
            continue
        name = row.get("name", "")
        long_b  = int(row.get("long_open_interest_balance") or 0)
        short_b = int(row.get("short_open_interest_balance") or 0)
        net = long_b - short_b
        if "外資" in name:
            result["futures_foreign_net"] += net
            found = True
        elif "投信" in name:
            result["futures_trust_net"] += net
            found = True
    return result if found else None

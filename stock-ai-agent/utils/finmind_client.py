"""
FinMind API 客戶端
負責三大法人買賣超、融資融券餘額兩類籌碼數據的抓取。
文件：https://finmindtrade.com/analysis/#/data/document
"""
import os
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

FINMIND_TOKEN = os.environ.get(
    "FINMIND_API_KEY",
    "eyJOeXAiOiJKV1QiLCJhbGciOiJIUzl1Nij9.eyJ1c2VyX2lkljoiTGkgbmluzylslmVtYWIsljoiMjA1OWJsQGdtYWlsLmNvbSIsInRva2VuX3ZlcnNpb24iOjB9.LqOpJ6_2UuEyGzuvBUtosDXW1kTJzlu2PtMjbamsRU",
)
_BASE = "https://api.finmindtrade.com/api/v4/data"


async def _fm_get(dataset: str, stock_code: str, start_date: str) -> list[dict]:
    params = {
        "dataset": dataset,
        "data_id": stock_code,
        "start_date": start_date,
        "token": FINMIND_TOKEN,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(_BASE, params=params)
            r.raise_for_status()
            data = r.json()
            if data.get("status") != 200:
                logger.warning(f"FinMind {dataset} {stock_code} 回應: {data.get('msg')}")
                return []
            return data.get("data", [])
    except Exception as e:
        logger.warning(f"FinMind API 失敗 {dataset} {stock_code}: {e}")
        return []


async def fetch_institutional(stock_code: str, trade_date: str) -> Optional[dict]:
    """
    三大法人買賣超 (TaiwanStockInstitutionalInvestorsBuySell)
    回傳 {foreign_net_buy, investment_trust_net_buy, dealer_net_buy}
    """
    rows = await _fm_get(
        "TaiwanStockInstitutionalInvestorsBuySell", stock_code, trade_date
    )
    # API 回傳當日所有法人分行，依 name 匯總
    result = {"foreign_net_buy": 0, "investment_trust_net_buy": 0, "dealer_net_buy": 0}
    name_map = {
        "外資": "foreign_net_buy",
        "外資自營": "foreign_net_buy",
        "投信": "investment_trust_net_buy",
        "自營商": "dealer_net_buy",
        "自營商(自行買賣)": "dealer_net_buy",
        "自營商(避險)": "dealer_net_buy",
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
    rows = await _fm_get(
        "TaiwanStockMarginPurchaseShortSale", stock_code, trade_date
    )
    for row in rows:
        if row.get("date") != trade_date:
            continue
        margin_balance = int(row.get("MarginPurchaseTodayBalance") or 0)
        short_shares   = int(row.get("ShortSaleTodayBalance") or 0)
        ratio = round(short_shares / margin_balance * 100, 2) if margin_balance > 0 else 0.0
        return {
            "margin_balance": margin_balance,
            "margin_short_shares": short_shares,
            "short_to_margin_ratio": ratio,
        }
    return None

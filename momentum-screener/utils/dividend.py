"""
TWSE 除權息日曆
資料來源：https://www.twse.com.tw/rwd/zh/exRight/TWT49U
"""
import httpx
import logging
import datetime

logger = logging.getLogger(__name__)

_TWSE_EXDIV = "https://www.twse.com.tw/rwd/zh/exRight/TWT49U"
_HEADERS    = {"User-Agent": "Mozilla/5.0 (compatible; StockScreener/2.2)"}


async def fetch_exdiv_calendar(
    start_date: datetime.date,
    end_date: datetime.date,
) -> list[dict]:
    """
    取得指定期間台股除權息資訊。
    回傳：[{ex_date, stock_code, stock_name, dividend_type,
             last_buy_date, cash_dividend, stock_dividend}]
    """
    params = {
        "response": "json",
        "strDate":  start_date.strftime("%Y%m%d"),
        "endDate":  end_date.strftime("%Y%m%d"),
    }
    try:
        async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as c:
            r    = await c.get(_TWSE_EXDIV, params=params)
            data = r.json()
    except Exception as e:
        logger.warning(f"除權息日曆抓取失敗: {e}")
        return []

    # TWSE 欄位順序：除息日、股票代號、股票名稱、除息/除權、最後買進日、...、現金股利、股票股利
    result = []
    for row in data.get("data", []):
        if len(row) < 5:
            continue
        result.append({
            "ex_date":       row[0].strip(),
            "stock_code":    row[1].strip(),
            "stock_name":    row[2].strip(),
            "dividend_type": row[3].strip(),   # 除息 / 除權 / 除息+除權
            "last_buy_date": row[4].strip(),   # 最後買進日
            "cash_dividend": row[6].strip() if len(row) > 6 else "",
            "stock_dividend": row[7].strip() if len(row) > 7 else "",
        })
    return result


async def fetch_upcoming_exdiv(
    stock_codes: list[str],
    days_ahead: int = 7,
) -> list[dict]:
    """
    篩選追蹤股中，未來 days_ahead 天內有除權息的標的。
    """
    _tz8  = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(_tz8).date()
    end   = today + datetime.timedelta(days=days_ahead)

    all_events = await fetch_exdiv_calendar(today, end)
    code_set   = set(stock_codes)

    result = []
    for ev in all_events:
        if ev["stock_code"] in code_set:
            result.append(ev)
    return result

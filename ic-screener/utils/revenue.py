"""
FinMind 月營收資料
Dataset: TaiwanStockMonthRevenue
"""
import os
import httpx
import logging
import datetime

logger = logging.getLogger(__name__)

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"


async def _fm_get(dataset: str, stock_code: str,
                  start: datetime.date, end: datetime.date) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(FINMIND_BASE, params={
                "dataset":    dataset,
                "data_id":    stock_code,
                "start_date": str(start),
                "end_date":   str(end),
                "token":      FINMIND_TOKEN,
            })
            data = r.json()
            if data.get("status") != 200:
                logger.warning(f"FinMind {dataset} {stock_code}: {data.get('msg')}")
                return []
            return data.get("data", [])
    except Exception as e:
        logger.warning(f"FinMind {dataset} {stock_code}: {e}")
        return []


async def fetch_monthly_revenue(stock_code: str, months: int = 12) -> list[dict]:
    """
    取得近 months 個月的月營收。
    回傳：[{date, revenue, revenue_year, revenue_month,
             revenue_year_growth, revenue_month_growth}]
    """
    end   = datetime.date.today()
    start = end.replace(year=end.year - 2)
    rows  = await _fm_get("TaiwanStockMonthRevenue", stock_code, start, end)
    rows  = sorted(rows, key=lambda x: x.get("date", ""))
    return rows[-months:]


async def calc_cumulative_growth(stock_code: str) -> dict:
    """
    計算今年累計營收 vs 去年同期累計成長率。
    回傳：{cum_growth_pct, months_compared, yoy_positive_months}
    """
    rows = await fetch_monthly_revenue(stock_code, months=24)
    if len(rows) < 2:
        return {"cum_growth_pct": None, "months_compared": 0, "yoy_positive_months": 0}

    now        = datetime.date.today()
    this_year  = now.year
    this_month = now.month

    # 今年已公告月份的累計（月營收通常延遲1個月，抓到上個月）
    this_yr_rows = [r for r in rows if int(r.get("revenue_year", 0)) == this_year]
    last_yr_rows = [r for r in rows if int(r.get("revenue_year", 0)) == this_year - 1]

    if not this_yr_rows or not last_yr_rows:
        return {"cum_growth_pct": None, "months_compared": 0, "yoy_positive_months": 0}

    # 取相同月份比較
    compared_months = [r["revenue_month"] for r in this_yr_rows]
    last_yr_same    = [r for r in last_yr_rows if r["revenue_month"] in compared_months]

    this_cum = sum(int(r.get("revenue", 0) or 0) for r in this_yr_rows)
    last_cum = sum(int(r.get("revenue", 0) or 0) for r in last_yr_same)

    if last_cum == 0:
        return {"cum_growth_pct": None, "months_compared": len(this_yr_rows), "yoy_positive_months": 0}

    growth     = (this_cum - last_cum) / last_cum * 100
    yoy_months = sum(
        1 for r in this_yr_rows
        if float(r.get("revenue_year_growth", 0) or 0) > 0
    )
    return {
        "cum_growth_pct":     round(growth, 2),
        "months_compared":    len(this_yr_rows),
        "yoy_positive_months": yoy_months,
    }

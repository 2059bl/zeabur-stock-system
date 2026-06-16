"""
FinMind 財報資料（EPS、ROE、獲利成長、股本）
Datasets: TaiwanStockFinancialStatements, TaiwanStockInfo
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


async def fetch_financials(stock_code: str) -> dict:
    """
    取得最新財報指標：EPS（Q1）、ROE、上半年獲利成長、負債比。
    回傳：{q1_eps, roe, h1_profit_growth_pct, debt_ratio,
            h1_profit_this, h1_profit_last, report_date}
    """
    end   = datetime.date.today()
    start = end.replace(year=end.year - 2)
    rows  = await _fm_get("TaiwanStockFinancialStatements", stock_code, start, end)

    if not rows:
        return {}

    # 依 date 排序
    rows = sorted(rows, key=lambda x: x.get("date", ""))

    def _val(row: dict, field: str) -> float:
        return float(row.get(field, 0) or 0)

    # 找 Q1（type = season, date 含 Q1 = 3月底）
    q1_rows = [r for r in rows if r.get("type") == "season" and "-03-" in r.get("date", "")]
    q1_eps  = _val(q1_rows[-1], "EPS") if q1_rows else None

    # ROE（最新一季）
    season_rows = [r for r in rows if r.get("type") == "season"]
    roe = _val(season_rows[-1], "ROE") if season_rows else None

    # 負債比（最新一季）
    debt_ratio = _val(season_rows[-1], "DebtRatio") if season_rows else None

    # 上半年獲利成長（H1 = Q1+Q2，type = cumulative，date 含 06-30）
    h1_rows = sorted(
        [r for r in rows if r.get("type") == "cumulative" and "-06-" in r.get("date", "")],
        key=lambda x: x.get("date", ""),
    )
    h1_growth = None
    h1_this   = None
    h1_last   = None
    if len(h1_rows) >= 2:
        h1_this = _val(h1_rows[-1], "NetIncome")
        h1_last = _val(h1_rows[-2], "NetIncome")
        if h1_last != 0:
            h1_growth = round((h1_this - h1_last) / abs(h1_last) * 100, 2)

    return {
        "q1_eps":              q1_eps,
        "roe":                 roe,
        "debt_ratio":          debt_ratio,
        "h1_profit_growth_pct": h1_growth,
        "h1_profit_this":      h1_this,
        "h1_profit_last":      h1_last,
        "report_date":         season_rows[-1].get("date") if season_rows else None,
    }


async def fetch_stock_capital(stock_code: str) -> float:
    """
    取得股本（億元）。使用 TaiwanStockInfo。
    """
    try:
        end   = datetime.date.today()
        start = end.replace(year=end.year - 1)
        rows  = await _fm_get("TaiwanStockInfo", stock_code, start, end)
        if not rows:
            return 0.0
        latest = sorted(rows, key=lambda x: x.get("date", ""))[-1]
        # CapitalStock 單位：元，轉換為億元
        capital = float(latest.get("CapitalStock", 0) or 0) / 1e8
        return round(capital, 2)
    except Exception as e:
        logger.warning(f"股本查詢失敗 {stock_code}: {e}")
        return 0.0

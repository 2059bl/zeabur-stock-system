"""
FinMind 財報資料（EPS、ROE、獲利成長、股本、負債比）
Datasets:
  TaiwanStockFinancialStatements  → EPS、H1 淨利
  TaiwanStockBalanceSheet         → 負債比、股本、股東權益
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


def _pick(rows: list[dict], type_name: str) -> list[dict]:
    """Filter rows by type field (tall-format FinMind data)."""
    return [r for r in rows if r.get("type") == type_name]


def _latest_val(rows: list[dict], type_name: str) -> float | None:
    matches = sorted(_pick(rows, type_name), key=lambda x: x.get("date", ""))
    return float(matches[-1]["value"]) if matches and matches[-1].get("value") is not None else None


async def fetch_financials(stock_code: str) -> dict:
    """
    取得最新財報指標：EPS（Q1）、ROE（估算）、H1 獲利成長、負債比。
    資料來源：TaiwanStockFinancialStatements + TaiwanStockBalanceSheet（tall format）
    """
    end   = datetime.date.today()
    start = end.replace(year=end.year - 2)

    income_rows, bs_rows = await _parallel_fetch(stock_code, start, end)

    if not income_rows and not bs_rows:
        return {}

    # ── EPS：Q1 = 日期含 -03- 的最新一筆 ─────────────────────────────────────
    eps_q1_rows = sorted(
        [r for r in _pick(income_rows, "EPS") if "-03-" in r.get("date", "")],
        key=lambda x: x["date"],
    )
    q1_eps = float(eps_q1_rows[-1]["value"]) if eps_q1_rows else None

    # ── 負債比：Liabilities_per（已是 %） ─────────────────────────────────────
    debt_ratio = _latest_val(bs_rows, "Liabilities_per")

    # ── 股東權益（最新） ──────────────────────────────────────────────────────
    equity_rows = sorted(_pick(bs_rows, "EquityAttributableToOwnersOfParent"),
                         key=lambda x: x.get("date", ""))
    equity = float(equity_rows[-1]["value"]) if equity_rows else None

    # ── ROE = (Q1淨利 × 4) / 股東權益（近似年化） ────────────────────────────
    net_income_q1_rows = sorted(
        [r for r in _pick(income_rows, "IncomeAfterTaxes") if "-03-" in r.get("date", "")],
        key=lambda x: x["date"],
    )
    roe = None
    if net_income_q1_rows and equity and equity > 0:
        ni_q1 = float(net_income_q1_rows[-1]["value"])
        roe   = round(ni_q1 * 4 / equity * 100, 2)

    # ── H1 獲利成長：IncomeAfterTaxes 日期含 -06- ─────────────────────────────
    h1_rows = sorted(
        [r for r in _pick(income_rows, "IncomeAfterTaxes") if "-06-" in r.get("date", "")],
        key=lambda x: x["date"],
    )
    h1_growth = None
    h1_this   = None
    h1_last   = None
    if len(h1_rows) >= 2:
        h1_this  = float(h1_rows[-1]["value"])
        h1_last  = float(h1_rows[-2]["value"])
        if h1_last and h1_last != 0:
            h1_growth = round((h1_this - h1_last) / abs(h1_last) * 100, 2)

    report_date = eps_q1_rows[-1]["date"] if eps_q1_rows else None

    return {
        "q1_eps":               q1_eps,
        "roe":                  roe,
        "debt_ratio":           debt_ratio,
        "h1_profit_growth_pct": h1_growth,
        "h1_profit_this":       h1_this,
        "h1_profit_last":       h1_last,
        "report_date":          report_date,
    }


async def _parallel_fetch(stock_code: str, start: datetime.date, end: datetime.date):
    """Fetch income statement and balance sheet concurrently."""
    import asyncio
    income, bs = await asyncio.gather(
        _fm_get("TaiwanStockFinancialStatements", stock_code, start, end),
        _fm_get("TaiwanStockBalanceSheet",        stock_code, start, end),
    )
    return income, bs


async def fetch_stock_capital(stock_code: str) -> float:
    """
    取得股本（億元）。
    使用 TaiwanStockBalanceSheet → type == "CapitalStock"，單位：元。
    """
    try:
        end   = datetime.date.today()
        start = end.replace(year=end.year - 2)
        rows  = await _fm_get("TaiwanStockBalanceSheet", stock_code, start, end)
        val   = _latest_val(rows, "CapitalStock")
        if val is None:
            return 0.0
        return round(val / 1e8, 2)
    except Exception as e:
        logger.warning(f"股本查詢失敗 {stock_code}: {e}")
        return 0.0

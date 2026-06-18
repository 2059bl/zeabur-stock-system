"""
FinMind 財報資料（EPS、ROE、獲利成長、股本、負債比）
Datasets:
  TaiwanStockFinancialStatements  → EPS、季度淨利
  TaiwanStockBalanceSheet         → 負債比、股本、股東權益

v1.1 改動：不再寫死 Q1（-03-）或 H1（-06-），
改為自動取最新可用季度，適用全年任何月份篩選。
"""
import os
import asyncio
import httpx
import logging
import datetime

logger = logging.getLogger(__name__)

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"

# FinMind 季報日期後綴：Q1=03, Q2=06, Q3=09, Q4=12
_QUARTER_MONTHS = ("-03-", "-06-", "-09-", "-12-")


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


def _is_quarter_row(row: dict) -> bool:
    """判斷是否為季報資料（日期含季末月份）。"""
    d = row.get("date", "")
    return any(m in d for m in _QUARTER_MONTHS)


def _latest_quarter_rows(rows: list[dict], type_name: str, n: int = 1) -> list[dict]:
    """
    取最新 n 筆季報資料（按日期降序）。
    只取日期含季末月份（03/06/09/12）的資料列。
    """
    quarters = sorted(
        [r for r in _pick(rows, type_name) if _is_quarter_row(r)],
        key=lambda x: x.get("date", ""),
        reverse=True,
    )
    return quarters[:n]


async def _parallel_fetch(stock_code: str, start: datetime.date, end: datetime.date):
    """Fetch income statement and balance sheet concurrently."""
    income, bs = await asyncio.gather(
        _fm_get("TaiwanStockFinancialStatements", stock_code, start, end),
        _fm_get("TaiwanStockBalanceSheet",        stock_code, start, end),
    )
    return income, bs


async def fetch_financials(stock_code: str) -> dict:
    """
    取得最新財報指標，自動使用最新可用季度（不寫死 Q1）。

    - EPS：最新一季
    - ROE：最新季淨利年化 / 最新股東權益
    - 獲利成長：最新季 vs 去年同季淨利
    - 負債比：最新資產負債表
    """
    end   = datetime.date.today()
    start = end.replace(year=end.year - 3)   # 抓3年確保有去年同季可比

    income_rows, bs_rows = await _parallel_fetch(stock_code, start, end)

    if not income_rows and not bs_rows:
        return {}

    # ── 最新季 EPS ────────────────────────────────────────────────────────────
    latest_eps_rows = _latest_quarter_rows(income_rows, "EPS", n=1)
    latest_eps      = float(latest_eps_rows[0]["value"]) if latest_eps_rows else None
    report_date     = latest_eps_rows[0]["date"] if latest_eps_rows else None

    # ── 負債比：Liabilities_per（已是 %，取最新） ─────────────────────────────
    debt_ratio = _latest_val(bs_rows, "Liabilities_per")

    # ── 股東權益（最新） ──────────────────────────────────────────────────────
    equity_rows = sorted(
        _pick(bs_rows, "EquityAttributableToOwnersOfParent"),
        key=lambda x: x.get("date", ""),
    )
    equity = float(equity_rows[-1]["value"]) if equity_rows else None

    # ── ROE = 最新季淨利年化 / 股東權益 ──────────────────────────────────────
    latest_ni_rows = _latest_quarter_rows(income_rows, "IncomeAfterTaxes", n=1)
    roe = None
    if latest_ni_rows and equity and equity > 0:
        ni_latest = float(latest_ni_rows[0]["value"])
        roe = round(ni_latest * 4 / equity * 100, 2)

    # ── 獲利成長：最新季 vs 去年同季（YoY） ──────────────────────────────────
    # 取最新2筆同月份季報（相差12個月）
    profit_growth = None
    profit_this   = None
    profit_last   = None

    if latest_ni_rows:
        latest_ni_date  = latest_ni_rows[0]["date"]          # e.g. "2025-06-30"
        latest_ni_month = latest_ni_date[4:7]                # e.g. "-06-"
        # 找去年同季（日期含同月份且比最新早至少10個月）
        same_q_rows = sorted(
            [r for r in _pick(income_rows, "IncomeAfterTaxes")
             if latest_ni_month in r.get("date", "") and r["date"] < latest_ni_date],
            key=lambda x: x["date"],
            reverse=True,
        )
        if same_q_rows:
            profit_this = float(latest_ni_rows[0]["value"])
            profit_last = float(same_q_rows[0]["value"])
            if profit_last and profit_last != 0:
                profit_growth = round((profit_this - profit_last) / abs(profit_last) * 100, 2)

    logger.debug(
        f"{stock_code} 財報: EPS={latest_eps} date={report_date} "
        f"ROE={roe}% 獲利YoY={profit_growth}% 負債比={debt_ratio}%"
    )

    return {
        "q1_eps":               latest_eps,        # 實為最新季 EPS（欄位名維持相容）
        "roe":                  roe,
        "debt_ratio":           debt_ratio,
        "h1_profit_growth_pct": profit_growth,     # 實為最新季 YoY 成長
        "h1_profit_this":       profit_this,
        "h1_profit_last":       profit_last,
        "report_date":          report_date,        # 最新季報日期
    }


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

"""
EPS 推估分析 — 三率三升個股財務推估
=====================================
推估邏輯：
  全年預估營收 = Q1實際 + Q2實際 + Q3預估(6+7+7月) + Q4預估(7月×3)
  全年 YoY = (2026預估 - 2025) / 2025 * 100%
  預估 EPS = 全年預估營收 × 最新Q2營業利益率 / 股本（百萬）
"""
import logging
from typing import Optional

from src.finmind_client import fm_get

logger = logging.getLogger(__name__)


async def _fetch_monthly_revenue(stock_code: str) -> list[dict]:
    rows = await fm_get("TaiwanStockMonthRevenue", stock_code, "2025-01-01")
    return sorted(rows, key=lambda r: (r.get("revenue_year", 0), r.get("revenue_month", 0)))


async def _fetch_financials(stock_code: str) -> list[dict]:
    """TaiwanStockFinancialStatements：含 EPS / Revenue / GrossProfit / OperatingIncome / IncomeAfterTaxes"""
    rows = await fm_get("TaiwanStockFinancialStatements", stock_code, "2024-01-01")
    return rows


def _get_quarterly_revenue(monthly: list[dict], year: int, quarter: int) -> Optional[float]:
    """取年度某季的月營收合計（百萬元）。"""
    month_map = {1: [1,2,3], 2: [4,5,6], 3: [7,8,9], 4: [10,11,12]}
    months = month_map.get(quarter, [])
    total = 0.0
    found = 0
    for r in monthly:
        if int(r.get("revenue_year", 0)) == year and int(r.get("revenue_month", 0)) in months:
            total += float(r.get("revenue", 0) or 0) / 1_000_000
            found += 1
    return total if found > 0 else None


def _get_annual_revenue(monthly: list[dict], year: int) -> Optional[float]:
    total = 0.0
    found = 0
    for r in monthly:
        if int(r.get("revenue_year", 0)) == year:
            total += float(r.get("revenue", 0) or 0) / 1_000_000
            found += 1
    return total if found >= 6 else None  # 至少半年才算


def _get_month_revenue(monthly: list[dict], year: int, month: int) -> Optional[float]:
    for r in monthly:
        if int(r.get("revenue_year", 0)) == year and int(r.get("revenue_month", 0)) == month:
            return float(r.get("revenue", 0) or 0) / 1_000_000
    return None


def _get_operating_margin(financials: list[dict]) -> Optional[float]:
    """最新 Q2 營業利益率（% 或 ppt）。"""
    q2_rows = [r for r in financials
               if r.get("type") == "OperatingIncome" and "Q2" in str(r.get("origin_name", ""))]
    rev_rows = [r for r in financials
                if r.get("type") == "Revenue" and "Q2" in str(r.get("origin_name", ""))]

    # 若無季報，嘗試用累計值推算
    if not q2_rows:
        q2_rows = [r for r in financials if r.get("type") == "OperatingIncome"]
    if not rev_rows:
        rev_rows = [r for r in financials if r.get("type") == "Revenue"]

    if not q2_rows or not rev_rows:
        return None

    latest_op  = max(q2_rows,  key=lambda r: r.get("date", ""))
    latest_rev = max(rev_rows, key=lambda r: r.get("date", ""))

    op  = float(latest_op.get("value", 0) or 0)
    rev = float(latest_rev.get("value", 1) or 1)
    if rev <= 0:
        return None
    return round(op / rev * 100, 2)


def _get_share_capital(financials: list[dict]) -> Optional[float]:
    """股本（百萬元）。"""
    rows = [r for r in financials if "StockCapital" in str(r.get("type", "")) or
            "股本" in str(r.get("origin_name", ""))]
    if not rows:
        return None
    latest = max(rows, key=lambda r: r.get("date", ""))
    val = float(latest.get("value", 0) or 0)
    # FinMind 回傳單位可能是元或千元，統一轉百萬
    if val > 1_000_000:
        return val / 1_000_000
    return val


async def analyze_eps(stock_code: str, stock_name: str) -> Optional[dict]:
    """
    對單一股票執行 EPS 推估。
    回傳 dict 或 None（資料不足）。
    """
    monthly    = await _fetch_monthly_revenue(stock_code)
    financials = await _fetch_financials(stock_code)

    if not monthly:
        logger.warning(f"{stock_code} 無月營收資料")
        return None

    # ── 2026 實際月份（Q1: 1-3月, Q2: 4-6月，7月是最新）──────────────────────
    q1_2026 = _get_quarterly_revenue(monthly, 2026, 1)
    q2_2026 = _get_quarterly_revenue(monthly, 2026, 2)
    rev_jul  = _get_month_revenue(monthly, 2026, 7)

    if q1_2026 is None and q2_2026 is None:
        logger.warning(f"{stock_code} 無 2026 年度資料")
        return None

    # ── Q3 預估：6月 + 7月 + 7月（7月為最新月） ──────────────────────────────
    rev_jun = _get_month_revenue(monthly, 2026, 6)
    if rev_jul is not None and rev_jun is not None:
        q3_est = rev_jun + rev_jul + rev_jul
    elif rev_jul is not None:
        q3_est = rev_jul * 3
    else:
        q3_est = None

    # ── Q4 預估：7月 × 3 ────────────────────────────────────────────────────
    q4_est = rev_jul * 3 if rev_jul is not None else None

    total_2026 = (
        (q1_2026 or 0) + (q2_2026 or 0) + (q3_est or 0) + (q4_est or 0)
    )
    if total_2026 <= 0:
        return None

    # ── 2025 全年營收（YoY 基準）────────────────────────────────────────────
    rev_2025 = _get_annual_revenue(monthly, 2025)
    rev_yoy  = None
    if rev_2025 and rev_2025 > 0:
        rev_yoy = round((total_2026 - rev_2025) / rev_2025 * 100, 1)

    # ── 預估 EPS ─────────────────────────────────────────────────────────────
    op_margin   = _get_operating_margin(financials)
    share_cap   = _get_share_capital(financials)
    eps_est     = None
    eps_yoy_pct = None

    if op_margin is not None and share_cap and share_cap > 0:
        # 營業淨利 ≈ 全年營收 × 營業利益率（百萬）
        net_income_est = total_2026 * (op_margin / 100)
        # 股數（億股）= 股本（百萬）/ 10（每股 10 元）
        shares_hundred_m = share_cap / 10
        eps_est = round(net_income_est / shares_hundred_m / 100, 2) if shares_hundred_m > 0 else None

        # 2025 EPS
        eps_rows = [r for r in financials if r.get("type") == "EPS"]
        if eps_rows:
            eps_2025_rows = [r for r in eps_rows if "2025" in str(r.get("date", ""))]
            if eps_2025_rows:
                eps_2025 = float(max(eps_2025_rows, key=lambda r: r.get("date", ""))
                                  .get("value", 0) or 0)
                if eps_2025 and eps_est is not None:
                    eps_yoy_pct = round((eps_est - eps_2025) / abs(eps_2025) * 100, 1) if eps_2025 != 0 else None

    # ── 月營收 N 月新高 ──────────────────────────────────────────────────────
    if rev_jul is not None:
        higher = 0
        for r in sorted(monthly, key=lambda x: (x.get("revenue_year", 0), x.get("revenue_month", 0)), reverse=True):
            yr = int(r.get("revenue_year", 0))
            mo = int(r.get("revenue_month", 0))
            if yr == 2026 and mo >= 7:
                continue
            v = float(r.get("revenue", 0) or 0) / 1_000_000
            if v < rev_jul:
                higher += 1
            else:
                break
        n_month_high = higher if higher > 0 else None
    else:
        n_month_high = None

    # ── 市場預期差（3個月均值為基準） ────────────────────────────────────────
    yoy_hist = []
    for r in sorted(monthly, key=lambda x: (x.get("revenue_year", 0), x.get("revenue_month", 0)), reverse=True)[:6]:
        v = r.get("revenue_year_growth")
        if v is not None:
            try:
                yoy_hist.append(float(v))
            except (ValueError, TypeError):
                pass
    latest_yoy = float(monthly[-1].get("revenue_year_growth", 0) or 0) if monthly else 0
    estimate_tag = None
    if len(yoy_hist) >= 3:
        expected = sum(yoy_hist[1:4]) / 3
        diff = latest_yoy - expected
        if diff >= 10:
            estimate_tag = f"遠高於預期+{diff:.1f}%"
        elif diff >= -10:
            estimate_tag = f"符合預期{diff:+.1f}%"
        else:
            estimate_tag = f"低於預期{diff:.1f}%"

    return {
        "code":           stock_code,
        "name":           stock_name,
        "rev_2026_est":   round(total_2026, 1),        # 百萬元
        "rev_2025":       round(rev_2025, 1) if rev_2025 else None,
        "rev_yoy_pct":    rev_yoy,
        "op_margin":      op_margin,
        "eps_est":        eps_est,
        "eps_yoy_pct":    eps_yoy_pct,
        "n_month_high":   n_month_high,
        "estimate_tag":   estimate_tag,
        "q1_2026":        round(q1_2026, 1) if q1_2026 else None,
        "q2_2026":        round(q2_2026, 1) if q2_2026 else None,
        "q3_est":         round(q3_est, 1)  if q3_est  else None,
        "q4_est":         round(q4_est, 1)  if q4_est  else None,
    }


async def run_eps_analysis(stocks: list[dict]) -> list[dict]:
    """批量 EPS 推估，回傳結果清單，依 eps_yoy_pct DESC 排序。"""
    import asyncio
    sem = asyncio.Semaphore(5)

    async def _one(s: dict):
        async with sem:
            result = await analyze_eps(s["code"], s.get("name", s["code"]))
            await asyncio.sleep(0.2)
            return result

    tasks   = [_one(s) for s in stocks]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    clean   = [r for r in results if isinstance(r, dict) and r is not None]
    clean.sort(key=lambda x: (x.get("eps_yoy_pct") or -9999), reverse=True)
    return clean


def to_markdown_table(results: list[dict]) -> str:
    """輸出 Markdown 表格。"""
    lines = [
        "| 代號/名稱 | 2026預估營收YoY% | 預估EPS增減% | 營收新高紀錄 | 市場預期差 | 綜合動能評價 |",
        "|-----------|----------------|------------|------------|-----------|------------|",
    ]
    for r in results:
        code      = r["code"]
        name      = r["name"]
        yoy       = f"{r['rev_yoy_pct']:+.1f}%" if r.get("rev_yoy_pct") is not None else "—"
        eps_yoy   = f"{r['eps_yoy_pct']:+.1f}%" if r.get("eps_yoy_pct") is not None else "—"
        n_high    = f"創{r['n_month_high']}月新高" if r.get("n_month_high") else "非新高"
        est_tag   = r.get("estimate_tag") or "—"
        # 15字內評語
        score_val = r.get("eps_yoy_pct") or 0
        n_h       = r.get("n_month_high") or 0
        if score_val >= 50 and n_h >= 12:
            comment = "⭐三率升+創年高強勁"
        elif score_val >= 30 and est_tag.startswith("遠高"):
            comment = "超預期EPS高成長"
        elif score_val >= 0 and n_h >= 6:
            comment = "穩健成長中高新高"
        elif score_val < -20:
            comment = "EPS衰退注意"
        elif est_tag and est_tag.startswith("低於"):
            comment = "低於預期待觀察"
        else:
            comment = "持平符合預期"
        lines.append(
            f"| {code} {name} | {yoy} | {eps_yoy} | {n_high} | {est_tag} | {comment} |"
        )
    return "\n".join(lines)

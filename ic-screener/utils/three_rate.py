"""
三率三升財務推估系統 v1.0
==========================
三率 = 毛利率 / 營業利益率 / 淨利率
三升 = 三率均較去年同期上升（YoY）

功能：
1. 從財報計算三率（最新季 vs 去年同季）
2. EPS 全年推估：H1實際 + H2估算（依 H2 去年 EPS × YoY成長率）
3. 市場動能評分：法人流向 + 月營收動能
4. 輸出 Markdown 表格，按 EPS成長率 降序排列
"""
import asyncio
import logging
import datetime
import os
import urllib.request
import urllib.parse
import json
from typing import Optional

logger = logging.getLogger(__name__)

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN") or os.environ.get("FINMIND_API_KEY", "")
FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"


# ── FinMind 同步輔助（三率分析需要完整歷史，避免 async 嵌套問題）──────────────

def _sync_fm_get(dataset: str, data_id: str, start: str, end: str) -> list[dict]:
    params = urllib.parse.urlencode({
        "dataset": dataset, "data_id": data_id,
        "start_date": start, "end_date": end, "token": FINMIND_TOKEN,
    })
    try:
        r = urllib.request.urlopen(f"{FINMIND_BASE}?{params}", timeout=20)
        d = json.loads(r.read().decode())
        if d.get("status") != 200:
            logger.warning(f"FinMind {dataset} {data_id}: {d.get('msg')}")
            return []
        return d.get("data", [])
    except Exception as e:
        logger.warning(f"FinMind {dataset} {data_id}: {e}")
        return []


def _pick(rows: list[dict], type_name: str) -> list[dict]:
    return sorted([r for r in rows if r.get("type") == type_name], key=lambda x: x.get("date", ""))


def _quarter_rows(rows: list[dict], type_name: str) -> list[dict]:
    QUARTER_M = ("-03-", "-06-", "-09-", "-12-")
    return [r for r in _pick(rows, type_name) if any(m in r.get("date", "") for m in QUARTER_M)]


def _val(row: dict) -> Optional[float]:
    v = row.get("value")
    return float(v) if v is not None else None


# ── 三率計算 ─────────────────────────────────────────────────────────────────

def calc_three_rates(income_rows: list[dict]) -> dict:
    """
    計算最新季三率 vs 去年同季，回傳：
    {
      quarter: 最新季日期,
      gross_margin:      毛利率(%),   gross_margin_yoy:      差值 ppt,
      operating_margin:  營業利益率(%),operating_margin_yoy:  差值 ppt,
      net_margin:        淨利率(%),   net_margin_yoy:         差值 ppt,
      three_rate_rise:   bool,  # 三率全升
      rise_count:        int,   # 幾率上升
    }
    """
    result = {
        "quarter": None,
        "gross_margin": None, "gross_margin_yoy": None,
        "operating_margin": None, "operating_margin_yoy": None,
        "net_margin": None, "net_margin_yoy": None,
        "three_rate_rise": False, "rise_count": 0,
    }

    rev_rows  = _quarter_rows(income_rows, "Revenue")
    gp_rows   = _quarter_rows(income_rows, "GrossProfit")
    oi_rows   = _quarter_rows(income_rows, "OperatingIncome")
    ni_rows   = _quarter_rows(income_rows, "IncomeAfterTaxes")

    if not rev_rows or not gp_rows or not oi_rows or not ni_rows:
        return result

    # 最新季
    latest_date = max(r["date"] for r in rev_rows + gp_rows + oi_rows + ni_rows
                      if r.get("value") is not None)
    latest_month = latest_date[4:7]  # e.g. "-06-"

    def get_q(rows, date):
        matches = [r for r in rows if r["date"] == date and r.get("value") is not None]
        return _val(matches[0]) if matches else None

    def get_prev_q(rows, current_date, month_suffix):
        # 去年同季 = 同月份但年份-1
        prev_year = str(int(current_date[:4]) - 1)
        candidates = [r for r in rows if r["date"].startswith(prev_year) and month_suffix in r["date"]]
        return _val(candidates[0]) if candidates else None

    rev = get_q(rev_rows, latest_date)
    if not rev or rev == 0:
        return result

    gp = get_q(gp_rows, latest_date)
    oi = get_q(oi_rows, latest_date)
    ni = get_q(ni_rows, latest_date)

    prev_rev = get_prev_q(rev_rows, latest_date, latest_month)
    prev_gp  = get_prev_q(gp_rows,  latest_date, latest_month)
    prev_oi  = get_prev_q(oi_rows,  latest_date, latest_month)
    prev_ni  = get_prev_q(ni_rows,  latest_date, latest_month)

    def rate(num, denom):
        return round(num / denom * 100, 2) if num is not None and denom and denom != 0 else None

    def yoy_diff(curr, prev):
        return round(curr - prev, 2) if curr is not None and prev is not None else None

    gm  = rate(gp, rev)
    om  = rate(oi, rev)
    nm  = rate(ni, rev)
    prev_gm = rate(prev_gp, prev_rev)
    prev_om = rate(prev_oi, prev_rev)
    prev_nm = rate(prev_ni, prev_rev)

    gm_yoy = yoy_diff(gm, prev_gm)
    om_yoy = yoy_diff(om, prev_om)
    nm_yoy = yoy_diff(nm, prev_nm)

    rise_count = sum(1 for v in [gm_yoy, om_yoy, nm_yoy] if v is not None and v > 0)

    result.update({
        "quarter": latest_date,
        "gross_margin": gm, "gross_margin_yoy": gm_yoy,
        "operating_margin": om, "operating_margin_yoy": om_yoy,
        "net_margin": nm, "net_margin_yoy": nm_yoy,
        "three_rate_rise": rise_count == 3,
        "rise_count": rise_count,
    })
    return result


# ── EPS 全年推估 ──────────────────────────────────────────────────────────────

def calc_eps_projection(income_rows: list[dict]) -> dict:
    """
    EPS 全年推估（當年適用）：
    - H1實際 = Q1 + Q2 EPS
    - H2去年實際 = Q3去年 + Q4去年 EPS
    - H2本年估算 = H2去年 × (1 + H1_YoY成長率)
    - 全年推估 = H1實際 + H2本年估算

    回傳: {h1_eps, h2_est_eps, full_year_eps_est, h1_yoy_pct, eps_growth_pct, h2_last_eps}
    """
    eps_rows = _quarter_rows(income_rows, "EPS")
    if not eps_rows:
        return {}

    now = datetime.date.today()
    this_year = str(now.year)
    last_year = str(now.year - 1)

    def eps_by_year_month(year, month_suffix):
        matches = [r for r in eps_rows
                   if r["date"].startswith(year) and month_suffix in r["date"]
                   and r.get("value") is not None]
        return _val(matches[0]) if matches else None

    # 今年 Q1/Q2
    q1_this = eps_by_year_month(this_year, "-03-")
    q2_this = eps_by_year_month(this_year, "-06-")

    # 去年 Q1/Q2/Q3/Q4
    q1_last = eps_by_year_month(last_year, "-03-")
    q2_last = eps_by_year_month(last_year, "-06-")
    q3_last = eps_by_year_month(last_year, "-09-")
    q4_last = eps_by_year_month(last_year, "-12-")

    # 今年實際已有的季度
    h1_this = None
    h1_last = None
    if q1_this is not None and q2_this is not None:
        h1_this = round(q1_this + q2_this, 2)
        if q1_last is not None and q2_last is not None:
            h1_last = round(q1_last + q2_last, 2)
    elif q1_this is not None:
        h1_this = round(q1_this, 2)
        if q1_last is not None:
            h1_last = round(q1_last, 2)

    if h1_this is None:
        return {}

    # H1 YoY 成長率
    h1_yoy = None
    if h1_last and h1_last != 0:
        h1_yoy = round((h1_this - h1_last) / abs(h1_last) * 100, 1)

    # H2 去年實際
    h2_last = None
    if q3_last is not None and q4_last is not None:
        h2_last = round(q3_last + q4_last, 2)

    # H2 本年估算
    h2_est = None
    if h2_last is not None and h1_yoy is not None:
        h2_est = round(h2_last * (1 + h1_yoy / 100), 2)
    elif h2_last is not None:
        h2_est = h2_last  # 無YoY時沿用去年H2

    # 全年推估
    full_est = None
    if h1_this is not None and h2_est is not None:
        full_est = round(h1_this + h2_est, 2)

    # 全年EPS成長率（vs 去年全年）
    last_full = None
    eps_growth = None
    if q1_last and q2_last and q3_last and q4_last:
        last_full = round(q1_last + q2_last + q3_last + q4_last, 2)
    if full_est and last_full and last_full != 0:
        eps_growth = round((full_est - last_full) / abs(last_full) * 100, 1)

    return {
        "h1_eps":            h1_this,
        "h1_yoy_pct":        h1_yoy,
        "h2_last_eps":       h2_last,
        "h2_est_eps":        h2_est,
        "full_year_eps_est": full_est,
        "last_year_eps":     last_full,
        "eps_growth_pct":    eps_growth,
    }


# ── 市場動能評分（0-10分）────────────────────────────────────────────────────

def calc_momentum_score(
    cum_rev_growth: Optional[float],
    yoy_positive_months: int,
    h1_yoy_pct: Optional[float],
    gross_margin_yoy: Optional[float],
    operating_margin_yoy: Optional[float],
) -> dict:
    """
    市場動能評分：
    Rev-M1  累積營收年成長 > 20%         → +2
    Rev-M2  近3個月皆正成長              → +2
    Rev-M3  累積年成長 > 10%（替補）      → +1
    Fin-M1  H1獲利成長 > 20%            → +2
    Fin-M2  毛利率YoY 上升               → +2
    Fin-M3  營業利益率YoY 上升            → +1
    總分 0~10
    """
    score = 0
    detail = []

    if cum_rev_growth is not None:
        if cum_rev_growth > 20:
            score += 2; detail.append("營收+20%↑(+2)")
        elif cum_rev_growth > 10:
            score += 1; detail.append("營收+10%↑(+1)")

    if yoy_positive_months >= 3:
        score += 2; detail.append("連3月正成長(+2)")
    elif yoy_positive_months >= 2:
        score += 1; detail.append("連2月正成長(+1)")

    if h1_yoy_pct is not None and h1_yoy_pct > 20:
        score += 2; detail.append(f"獲利+{h1_yoy_pct:.0f}%(+2)")
    elif h1_yoy_pct is not None and h1_yoy_pct > 0:
        score += 1; detail.append(f"獲利+{h1_yoy_pct:.0f}%(+1)")

    if gross_margin_yoy is not None and gross_margin_yoy > 0:
        score += 2; detail.append("毛利率↑(+2)")

    if operating_margin_yoy is not None and operating_margin_yoy > 0:
        score += 1; detail.append("營利率↑(+1)")

    return {"momentum_score": min(score, 10), "momentum_detail": ", ".join(detail)}


# ── 主函式：分析單一股票 ─────────────────────────────────────────────────────

def analyze_stock(code: str, name: str) -> Optional[dict]:
    """分析單一股票，回傳完整三率三升評估 dict，失敗回傳 None。"""
    end_date = datetime.date.today()
    start_date = end_date.replace(year=end_date.year - 3)

    income_rows = _sync_fm_get(
        "TaiwanStockFinancialStatements", code,
        str(start_date), str(end_date)
    )
    if not income_rows:
        return None

    rev_data = _sync_rev_data(code)
    three_rates = calc_three_rates(income_rows)
    eps_proj = calc_eps_projection(income_rows)

    cum_growth = rev_data.get("cum_growth_pct")
    yoy_pos_months = rev_data.get("yoy_positive_months", 0)
    momentum = calc_momentum_score(
        cum_growth,
        yoy_pos_months,
        eps_proj.get("h1_yoy_pct"),
        three_rates.get("gross_margin_yoy"),
        three_rates.get("operating_margin_yoy"),
    )

    return {
        "code": code,
        "name": name,
        # 三率
        "quarter":            three_rates.get("quarter"),
        "gross_margin":       three_rates.get("gross_margin"),
        "gross_margin_yoy":   three_rates.get("gross_margin_yoy"),
        "operating_margin":   three_rates.get("operating_margin"),
        "operating_margin_yoy": three_rates.get("operating_margin_yoy"),
        "net_margin":         three_rates.get("net_margin"),
        "net_margin_yoy":     three_rates.get("net_margin_yoy"),
        "three_rate_rise":    three_rates.get("three_rate_rise", False),
        "rise_count":         three_rates.get("rise_count", 0),
        # 營收動能
        "cum_rev_growth":     cum_growth,
        "yoy_positive_months": yoy_pos_months,
        # EPS 推估
        "h1_eps":             eps_proj.get("h1_eps"),
        "h1_yoy_pct":         eps_proj.get("h1_yoy_pct"),
        "h2_est_eps":         eps_proj.get("h2_est_eps"),
        "full_year_eps_est":  eps_proj.get("full_year_eps_est"),
        "last_year_eps":      eps_proj.get("last_year_eps"),
        "eps_growth_pct":     eps_proj.get("eps_growth_pct"),
        # 動能評分
        "momentum_score":     momentum["momentum_score"],
        "momentum_detail":    momentum["momentum_detail"],
    }


def _sync_rev_data(code: str) -> dict:
    """同步抓月營收，計算累積成長和連續正成長月數。"""
    end = datetime.date.today()
    start = end.replace(year=end.year - 2)
    rows = _sync_fm_get("TaiwanStockMonthRevenue", code, str(start), str(end))
    if not rows:
        return {"cum_growth_pct": None, "yoy_positive_months": 0}

    rows = sorted(rows, key=lambda x: x.get("date", ""))
    this_year = str(end.year)
    last_year = str(end.year - 1)

    this_yr = [r for r in rows if r.get("revenue_year") and str(r["revenue_year"]) == this_year]
    last_yr = [r for r in rows if r.get("revenue_year") and str(r["revenue_year"]) == last_year]

    cum_growth = None
    if this_yr and last_yr:
        compared_months = [r["revenue_month"] for r in this_yr]
        last_same = [r for r in last_yr if r["revenue_month"] in compared_months]
        this_cum = sum(int(r.get("revenue", 0) or 0) for r in this_yr)
        last_cum = sum(int(r.get("revenue", 0) or 0) for r in last_same)
        if last_cum > 0:
            cum_growth = round((this_cum - last_cum) / last_cum * 100, 2)

    yoy_pos = sum(1 for r in this_yr[-3:] if float(r.get("revenue_year_growth", 0) or 0) > 0)

    return {"cum_growth_pct": cum_growth, "yoy_positive_months": yoy_pos}


# ── Markdown 輸出 ─────────────────────────────────────────────────────────────

def fmt_pct(v, show_plus=True) -> str:
    if v is None: return "—"
    sign = "+" if v > 0 and show_plus else ""
    return f"{sign}{v:.1f}%"

def fmt_yoy(v) -> str:
    if v is None: return "—"
    arrow = "↑" if v > 0 else ("↓" if v < 0 else "→")
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f}ppt {arrow}"


def to_markdown(results: list[dict]) -> str:
    """輸出 Markdown 表格，按 EPS成長率 降序，三率全升標 ⭐。"""
    sorted_results = sorted(
        results,
        key=lambda x: (x.get("eps_growth_pct") or -999),
        reverse=True
    )

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"## 三率三升財務推估報告",
        f"*生成時間：{now}　|　共 {len(results)} 檔分析，⭐ = 三率全升*",
        "",
        "| 代號 | 名稱 | 季報 | 毛利率 | YoY | 營利率 | YoY | 淨利率 | YoY | H1 EPS | H1成長 | 全年EPS估 | EPS成長 | 動能分 |",
        "|------|------|------|--------|-----|--------|-----|--------|-----|--------|--------|-----------|---------|--------|",
    ]

    for r in sorted_results:
        star = "⭐" if r.get("three_rate_rise") else (f"({r.get('rise_count',0)}/3)")
        q = (r.get("quarter") or "")[:7]  # YYYY-MM
        gm  = fmt_pct(r.get("gross_margin"), show_plus=False)
        gmy = fmt_yoy(r.get("gross_margin_yoy"))
        om  = fmt_pct(r.get("operating_margin"), show_plus=False)
        omy = fmt_yoy(r.get("operating_margin_yoy"))
        nm  = fmt_pct(r.get("net_margin"), show_plus=False)
        nmy = fmt_yoy(r.get("net_margin_yoy"))
        h1  = f"{r['h1_eps']:.2f}" if r.get("h1_eps") is not None else "—"
        h1g = fmt_pct(r.get("h1_yoy_pct"))
        fy  = f"{r['full_year_eps_est']:.2f}" if r.get("full_year_eps_est") is not None else "—"
        epsg = fmt_pct(r.get("eps_growth_pct"))
        ms  = r.get("momentum_score", 0)
        lines.append(
            f"| {r['code']} {star} | {r['name']} | {q} | {gm} | {gmy} | {om} | {omy} | {nm} | {nmy} | {h1} | {h1g} | {fy} | {epsg} | {ms}/10 |"
        )

    # 動能評分明細（附表）
    lines += ["", "### 動能評分明細", ""]
    for r in sorted_results:
        if r.get("momentum_detail"):
            lines.append(f"- **{r['code']} {r['name']}**：{r['momentum_detail']}")

    return "\n".join(lines)

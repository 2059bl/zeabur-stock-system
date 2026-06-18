"""
產業委屈股篩選引擎 v1.1
=========================
雙層篩選架構：
  Layer 1：硬性門檻（6條，全過才進 Layer 2）
  Layer 2：加分條件（8條，每條 1-2 分，達 5 分以上入選）

Layer 1 硬性門檻（預設值，各池可透過 pool_cfg 覆寫）：
  H1  累計營收年成長 > cum_growth_min%  (預設 30%)
  H2  Q1 EPS > 0
  H3  本益比 < pe_max 倍               (預設 20)
  H4  股本 > capital_min 億            (預設 20)
  H5  近3交易日均量 > vol_min 張        (預設 1000)
  H6  負債比 < debt_max%              (預設 50%)

Layer 2 加分條件：
  S1  上半年獲利成長 > 30%         → +2分
  S2  近3月皆營收年成長            → +1分
  S3  外資近1週轉買超              → +2分（FinMind，選配）
  S4  股價距52週高點 > 20%         → +1分（被低估）
  S5  ROE > 10%                   → +1分
  S6  近1月漲幅介於 6%~25%         → +1分（啟動但未過熱）
  S7  法人持股比例 < 30%           → +1分（未被充分發現）
  S8  近1季漲幅 < 30%              → +1分（未追高）
"""
import asyncio
import logging
import datetime
from typing import Optional

from utils.db         import fetch_all, execute
from utils.revenue    import fetch_monthly_revenue, calc_cumulative_growth
from utils.financials import fetch_financials, fetch_stock_capital
from utils.price      import fetch_price_metrics

logger = logging.getLogger(__name__)

SCORE_THRESHOLD = 5   # Layer 2 最低入選分數

# 各產業池預設門檻覆寫（在 main.py _IC_POOLS cfg 設定）
_DEFAULT_CFG = {
    "cum_growth_min": 30,   # H1 累積營收年成長下限 (%)
    "pe_max":         20,   # H3 本益比上限
    "capital_min":    20,   # H4 股本下限 (億)
    "vol_min":        1000, # H5 3日均量下限 (張)
    "debt_max":       50,   # H6 負債比上限 (%)
}


async def screen_one(
    stock: dict,
    trade_date: datetime.date,
    pool_cfg: dict | None = None,
) -> Optional[dict]:
    """對單一股票執行雙層篩選，回傳結果 dict 或 None（未過 Layer 1）。"""
    code = stock["stock_code"]
    name = stock["stock_name"]

    cfg = {**_DEFAULT_CFG, **(pool_cfg or {})}
    cum_growth_min = cfg["cum_growth_min"]
    pe_max         = cfg["pe_max"]
    capital_min    = cfg["capital_min"]
    vol_min        = cfg["vol_min"]
    debt_max       = cfg["debt_max"]

    # 並行抓取所有資料
    price_data, rev_data, fin_data, capital = await asyncio.gather(
        fetch_price_metrics(code),
        calc_cumulative_growth(code),
        fetch_financials(code),
        fetch_stock_capital(code),
    )

    fails = []

    # ── Layer 1：硬性門檻 ────────────────────────────────────────────────────
    cum_growth = rev_data.get("cum_growth_pct")
    if cum_growth is None or cum_growth < cum_growth_min:
        fails.append(f"H1累積營收{cum_growth}%<{cum_growth_min}%")

    q1_eps      = fin_data.get("q1_eps")
    report_date = fin_data.get("report_date", "?")
    if q1_eps is None or q1_eps <= 0:
        fails.append(f"H2 EPS={q1_eps}≤0 ({report_date})")

    pe = None
    if price_data and q1_eps and q1_eps > 0:
        annualized_eps = q1_eps * 4
        pe = round(price_data["close"] / annualized_eps, 1)
    if pe is None or pe >= pe_max:
        fails.append(f"H3 PE={pe}≥{pe_max}")

    if capital < capital_min:
        fails.append(f"H4 股本{capital}億<{capital_min}億")

    avg_vol = price_data["avg_vol_3d"] if price_data else 0
    if avg_vol < vol_min:
        fails.append(f"H5 均量{avg_vol:.0f}張<{vol_min}張")

    debt = fin_data.get("debt_ratio")
    if debt is None or debt >= debt_max:
        fails.append(f"H6 負債比{debt}%≥{debt_max}%")

    if fails:
        logger.debug(f"{code} Layer1 未過：{'; '.join(fails)}")
        return None

    # ── Layer 2：加分條件 ─────────────────────────────────────────────────────
    score   = 0
    reasons = []

    # S1 上半年獲利成長 > 30%
    h1g = fin_data.get("h1_profit_growth_pct")
    if h1g is not None and h1g > 30:
        score += 2
        reasons.append(f"上半年獲利+{h1g:.0f}%(+2)")

    # S2 近3月皆營收年成長
    yoy_months = rev_data.get("yoy_positive_months", 0)
    compared   = rev_data.get("months_compared", 0)
    if compared >= 3 and yoy_months >= 3:
        score += 1
        reasons.append("近3月營收年增(+1)")

    # S3 外資近1週轉買（留空接口）
    # if foreign_net_1w > 0: score += 2; reasons.append(...)

    # S4 距52週高點跌幅 > 20%（dist_high_pct 為負數）
    dist = price_data.get("dist_high_pct", 0) if price_data else 0
    if dist <= -20:
        score += 1
        reasons.append(f"距高點跌{abs(dist):.0f}%(+1)")

    # S5 ROE > 10%
    roe = fin_data.get("roe")
    if roe is not None and roe > 10:
        score += 1
        reasons.append(f"ROE {roe:.1f}%(+1)")

    # S6 近1月漲幅 6%~25%（啟動但未過熱）
    m1 = price_data.get("m1_pct") if price_data else None
    if m1 is not None and 6 <= m1 <= 25:
        score += 1
        reasons.append(f"月漲{m1:.1f}%(+1)")

    # S7 法人持股（留空接口）

    # S8 近1季漲幅 < 30%
    q1p = price_data.get("q1_pct") if price_data else None
    if q1p is not None and q1p < 30:
        score += 1
        reasons.append(f"季漲{q1p:.1f}%<30%(+1)")

    if score < SCORE_THRESHOLD:
        logger.debug(f"{code} Layer2 分數不足：{score}/{SCORE_THRESHOLD}")
        return None

    logger.info(f"{code} {name} 入選！得分={score}  {'; '.join(reasons)}")
    return {
        "stock_code":        code,
        "stock_name":        name,
        "pool_id":           stock.get("pool_id"),
        "screen_date":       trade_date,
        "score":             score,
        "pe_ratio":          pe,
        "cum_rev_growth":    cum_growth,
        "q1_eps":            q1_eps,
        "h1_profit_growth":  h1g,
        "roe":               roe,
        "debt_ratio":        debt,
        "capital_bn":        capital,
        "close":             price_data["close"] if price_data else None,
        "avg_vol_3d":        avg_vol,
        "m1_pct":            m1,
        "q1_pct":            q1p,
        "dist_high_pct":     dist,
        "score_reasons":     "; ".join(reasons),
    }


async def run_screening(
    pool_id: int,
    trade_date: datetime.date,
    pool_cfg: dict | None = None,
) -> list[dict]:
    """對指定產業池執行完整篩選，回傳入選股清單（按分數降序）。"""
    stocks = await fetch_all("""
        SELECT ps.stock_code, ps.pool_id,
               COALESCE(s.stock_name, ps.stock_code) AS stock_name
        FROM screener_pool_stocks ps
        LEFT JOIN screener_stocks s ON s.stock_code = ps.stock_code
        WHERE ps.pool_id = $1 AND ps.is_active = TRUE
    """, pool_id)

    if not stocks:
        logger.warning(f"Pool {pool_id} 無啟用股票")
        return []

    logger.info(f"[Pool {pool_id}] 開始篩選 {len(stocks)} 檔 cfg={pool_cfg}")
    sem = asyncio.Semaphore(5)

    async def _one(s):
        async with sem:
            try:
                return await screen_one(s, trade_date, pool_cfg=pool_cfg)
            except Exception as e:
                logger.warning(f"{s['stock_code']} 篩選失敗: {e}")
                return None

    results = await asyncio.gather(*[_one(s) for s in stocks])
    passed  = [r for r in results if r is not None]
    passed.sort(key=lambda x: -x["score"])
    logger.info(f"[Pool {pool_id}] 入選 {len(passed)} 檔")
    return passed


async def save_results(candidates: list[dict]):
    """將篩選結果寫入 screener_results 表。"""
    for rank, c in enumerate(candidates, 1):
        await execute("""
            INSERT INTO screener_results
                (pool_id, screen_date, rank, stock_code, stock_name,
                 score, pe_ratio, cum_rev_growth, q1_eps, h1_profit_growth,
                 roe, debt_ratio, capital_bn, close_price,
                 avg_vol_3d, m1_pct, q1_pct, dist_high_pct, score_reasons)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
            ON CONFLICT (pool_id, screen_date, stock_code) DO UPDATE SET
                rank             = EXCLUDED.rank,
                score            = EXCLUDED.score,
                score_reasons    = EXCLUDED.score_reasons,
                updated_at       = NOW()
        """,
        c["pool_id"], c["screen_date"], rank,
        c["stock_code"], c["stock_name"],
        c["score"], c["pe_ratio"], c["cum_rev_growth"], c["q1_eps"],
        c["h1_profit_growth"], c["roe"], c["debt_ratio"], c["capital_bn"],
        c["close"], c["avg_vol_3d"], c["m1_pct"], c["q1_pct"],
        c["dist_high_pct"], c["score_reasons"])

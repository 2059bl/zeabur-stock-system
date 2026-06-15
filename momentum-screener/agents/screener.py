"""
尾盤動量突破短線篩選策略（7 道過濾器）
==========================================
Step 1  漲幅過濾      3% ≤ daily_return ≤ 5%           (可調)
Step 2  量比過濾      volume_ratio > 1.0                (可調)
Step 3  換手率過濾    5% ≤ turnover_rate ≤ 10%          (可調：需 shares_outstanding)
Step 4  流通市值過濾  250億 ≤ float_mktcap ≤ 2500億     (可調)
Step 5  量能形態識別  近 3 日量能遞增（允許 ±5% 容差）
Step 6  均線多頭排列  Price > MA5 > MA10 > MA20 > MA60
        + MA5/MA10/MA20 斜率均 > 0.1%（持續向上）
        + RSI < 75（剔除嚴重超買）
Step 7  相對強度      個股漲幅 > 大盤漲幅
排序    綜合分 = vol_ratio×0.40 + rel_strength_score×0.35 + turnover_score×0.25
"""
import logging
import asyncio
import datetime
from typing import Optional

from utils.db import fetch_all, execute
from utils.price_fetcher import fetch_stock_data, fetch_market_return

logger = logging.getLogger(__name__)

# ── 可調參數（集中管理） ────────────────────────────────────────────────────────
CFG = {
    # Step 1
    "return_min":        0.03,
    "return_max":        0.05,
    # Step 2
    "vol_ratio_min":     1.0,
    # Step 3
    "turnover_min":      0.05,
    "turnover_max":      0.10,
    # Step 4
    "mktcap_min":        25e9,
    "mktcap_max":        250e9,
    # Step 5
    "vol_slope_tolerance": 0.05,   # 允許 5% 誤差
    # Step 6
    "ma_slope_min":      0.001,    # 0.1%
    "rsi_max":           75,
    # 輸出
    "top_n":             10,
}


def _rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0.0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0.0 for d in deltas[-period:]]
    avg_g  = sum(gains) / period
    avg_l  = sum(losses) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def _turnover_score(rate: Optional[float]) -> float:
    """換手率分數：在 [5%,10%] 時線性 0→1，超出範圍 0。"""
    if rate is None:
        return 0.5   # 無資料時給中間值
    lo, hi = CFG["turnover_min"], CFG["turnover_max"]
    mid = (lo + hi) / 2
    if lo <= rate <= hi:
        return 1.0 - abs(rate - mid) / (hi - mid)
    return 0.0


async def run_screening(trade_date: datetime.date) -> list[dict]:
    """
    主篩選流程。
    回傳通過 7 道過濾器且排序後的前 top_n 檔股票。
    """
    # ── 取股票池（所有 is_active 股票） ────────────────────────────────────────
    stocks = await fetch_all(
        "SELECT stock_code, stock_name, market, sector, "
        "shares_outstanding, float_shares "
        "FROM stocks WHERE is_active = TRUE"
    )
    if not stocks:
        logger.warning("股票池為空，請先新增追蹤股票")
        return []

    # ── 大盤漲幅 ────────────────────────────────────────────────────────────────
    market_return = await fetch_market_return(trade_date)
    logger.info(f"[Screener] 大盤漲幅={market_return:.2%}，共 {len(stocks)} 檔待篩選")

    # ── 並行抓取個股數據 ─────────────────────────────────────────────────────────
    sem = asyncio.Semaphore(10)

    async def _fetch_one(s):
        async with sem:
            return s, await fetch_stock_data(s["stock_code"])

    results = await asyncio.gather(*[_fetch_one(s) for s in stocks])

    passed = []

    for stock, data in results:
        code  = stock["stock_code"]
        name  = stock["stock_name"]
        if data is None:
            continue

        ret         = data["daily_return"]
        vol_ratio   = data["volume_ratio"]
        closes      = data["closes"]
        vols_3d     = data["volumes_3d"]
        ma5, ma10   = data["ma5"], data["ma10"]
        ma20, ma60  = data["ma20"], data["ma60"]
        price       = data["close"]

        # ── Step 1: 漲幅 3%~5% ──────────────────────────────────────────────
        if not (CFG["return_min"] <= ret <= CFG["return_max"]):
            continue

        # ── Step 2: 量比 > 1.0 ──────────────────────────────────────────────
        if vol_ratio is None or vol_ratio < CFG["vol_ratio_min"]:
            continue

        # ── Step 3: 換手率 5%~10%（需 float_shares） ────────────────────────
        float_sh  = stock.get("float_shares") or stock.get("shares_outstanding")
        turnover  = None
        if float_sh and float_sh > 0 and data["volume_today"] > 0:
            turnover = data["volume_today"] / float_sh
        if turnover is not None and not (CFG["turnover_min"] <= turnover <= CFG["turnover_max"]):
            continue

        # ── Step 4: 流通市值 250億~2500億 ───────────────────────────────────
        float_mktcap = price * (float_sh or 0) * 1000  # 張 → 股（×1000）
        if float_sh and float_sh > 0:
            if not (CFG["mktcap_min"] <= float_mktcap <= CFG["mktcap_max"]):
                continue

        # ── Step 5: 量能台階遞增（允許 ±5% 容差）───────────────────────────
        if len(vols_3d) == 3:
            v0, v1, v2 = vols_3d          # v0=t-2, v1=t-1, v2=t
            tol = CFG["vol_slope_tolerance"]
            rising = (v1 >= v0 * (1 - tol)) and (v2 >= v1 * (1 - tol)) and (v2 > v0)
            if not rising:
                continue

        # ── Step 6a: 均線多頭排列 ────────────────────────────────────────────
        if None in (ma5, ma10, ma20):
            continue
        if not (price > ma5 > ma10 > ma20):
            continue
        if ma60 and ma20 <= ma60:
            continue

        # ── Step 6b: MA 斜率均向上 ───────────────────────────────────────────
        slopes_ok = all(
            (s is not None and s > CFG["ma_slope_min"])
            for s in (data["ma5_slope"], data["ma10_slope"], data["ma20_slope"])
        )
        if not slopes_ok:
            continue

        # ── Step 6c: RSI < 75（剔除嚴重超買）────────────────────────────────
        rsi_val = _rsi(closes)
        if rsi_val is not None and rsi_val >= CFG["rsi_max"]:
            continue

        # ── Step 7: 相對強度 > 0（跑贏大盤）────────────────────────────────
        rel_strength = ret - market_return
        if rel_strength <= 0:
            continue

        # ── 綜合評分 ─────────────────────────────────────────────────────────
        t_score   = _turnover_score(turnover)
        rs_score  = min(rel_strength / 0.05, 1.0)   # 相對超漲 5% 時滿分
        vr_score  = min((vol_ratio - 1) / 2.0, 1.0) # 量比 3× 時滿分
        composite = vr_score * 0.40 + rs_score * 0.35 + t_score * 0.25

        passed.append({
            "stock_code":      code,
            "stock_name":      name,
            "sector":          stock.get("sector", ""),
            "daily_return":    round(ret, 4),
            "volume_ratio":    round(vol_ratio, 2),
            "turnover_rate":   round(turnover, 4) if turnover else None,
            "float_mktcap_bn": round(float_mktcap / 1e9, 1) if float_sh else None,
            "relative_strength": round(rel_strength, 4),
            "rsi_14":          round(rsi_val, 1) if rsi_val else None,
            "ma5":             ma5,
            "ma20":            ma20,
            "composite_score": round(composite, 4),
            "screen_date":     trade_date,
        })

    # ── 排序：綜合分降序，取前 top_n ────────────────────────────────────────────
    passed.sort(key=lambda x: x["composite_score"], reverse=True)
    final = passed[:CFG["top_n"]]

    logger.info(f"[Screener] 通過篩選：{len(passed)} 檔，輸出前 {len(final)} 檔")
    return final


async def save_results(candidates: list[dict]):
    """將篩選結果寫入 screening_results 表。"""
    if not candidates:
        return
    for rank, c in enumerate(candidates, 1):
        await execute("""
            INSERT INTO screening_results
                (screen_date, rank, stock_code, stock_name, sector,
                 daily_return, volume_ratio, turnover_rate, float_mktcap_bn,
                 relative_strength, rsi_14, composite_score)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (screen_date, stock_code) DO UPDATE SET
                rank             = EXCLUDED.rank,
                composite_score  = EXCLUDED.composite_score,
                updated_at       = NOW()
        """,
        c["screen_date"], rank,
        c["stock_code"], c["stock_name"], c["sector"],
        c["daily_return"], c["volume_ratio"],
        c["turnover_rate"], c["float_mktcap_bn"],
        c["relative_strength"], c["rsi_14"], c["composite_score"])

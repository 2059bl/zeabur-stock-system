"""
momentum_agent — 尾盤動量突破短線篩選策略
每日 22:00 自動執行，7 道過濾器，輸出次日監控自選股池。

策略邏輯：
  Step 1  漲幅過濾        2% ~ 9%（台股尾盤有效動能區間）
  Step 2  量比過濾        今日量 / 5日均量 > 1.2（優化：原1.0太低）
  Step 3  換手率過濾      3% ~ 12%（寬鬆版，原5~10易漏掉好股）
  Step 4  流通市值過濾    250億 ~ 3000億（25B~300B TWD）
  Step 5  台階式放量      近3日量遞增（允許±10%誤差）
  Step 6  均線多頭排列    Price > MA5 > MA10 > MA20 > MA60，且MA5/10/20斜率>0
  Step 7  相對強度        個股漲幅 - 大盤漲幅 > 0
  加分項  RSI < 75        避免過熱追漲
  加分項  MACD柱狀 > 0    動能方向確認

輸出：按綜合評分降序，前5~10檔寫入 momentum_candidates 表，
      同時推播 Telegram，次日收盤後自動回算報酬率。
"""
import logging
from datetime import date as _date, timedelta

from utils.db import execute, fetch_all, fetch_one

logger = logging.getLogger(__name__)

# ── 可調參數（集中管理，方便微調）─────────────────────────────────────────────
CFG = {
    "return_min":        0.02,   # 最低漲幅 2%
    "return_max":        0.09,   # 最高漲幅 9%（避開當日過熱）
    "volume_ratio_min":  1.2,    # 量比下限
    "turnover_min":      0.03,   # 換手率下限 3%
    "turnover_max":      0.12,   # 換手率上限 12%
    "mktcap_min":        25e9,   # 流通市值下限 250億
    "mktcap_max":        300e9,  # 流通市值上限 3000億
    "rsi_max":           75,     # RSI 上限（避免過熱）
    "top_n":             10,     # 最終輸出檔數
    "vol_slope_tolerance": 0.10, # 台階量允許誤差 ±10%
}


# ── 資料取得 ──────────────────────────────────────────────────────────────────

async def _get_stock_universe(trade_date: str) -> list[dict]:
    """取當日所有有報價且有技術指標的股票。"""
    td = _date.fromisoformat(trade_date)
    return await fetch_all("""
        SELECT
            p.stock_code,
            s.stock_name,
            s.sector,
            s.market,
            p.close_price::float       AS close_price,
            p.volume::float            AS volume,
            p.change_pct::float        AS change_pct,
            i.sma_5::float             AS sma_5,
            i.sma_10::float            AS sma_10,
            i.sma_20::float            AS sma_20,
            i.sma_60::float            AS sma_60,
            i.rsi_14::float            AS rsi_14,
            i.macd_histogram::float    AS macd_histogram,
            i.foreign_net_buy          AS foreign_net_buy,
            s.shares_outstanding::float AS shares_outstanding,
            s.float_shares::float       AS float_shares
        FROM stock_prices p
        JOIN stocks s ON s.stock_code = p.stock_code
        JOIN stock_indicators i ON i.stock_code = p.stock_code AND i.trade_date = p.trade_date
        WHERE p.trade_date = $1
          AND s.is_active = TRUE
          AND p.close_price IS NOT NULL
    """, td)


async def _get_prev_close(stock_code: str, trade_date: str) -> float | None:
    """前一交易日收盤價。"""
    td = _date.fromisoformat(trade_date)
    row = await fetch_one("""
        SELECT close_price::float AS c FROM stock_prices
        WHERE stock_code = $1 AND trade_date < $2
        ORDER BY trade_date DESC LIMIT 1
    """, stock_code, td)
    return row["c"] if row else None


async def _get_volume_history(stock_code: str, trade_date: str, days: int = 6) -> list[float]:
    """近 N 日成交量（含今日，降序）。"""
    td = _date.fromisoformat(trade_date)
    rows = await fetch_all("""
        SELECT volume::float AS v FROM stock_prices
        WHERE stock_code = $1 AND trade_date <= $2
        ORDER BY trade_date DESC LIMIT $3
    """, stock_code, td, days)
    return [r["v"] for r in rows]


async def _get_sma_history(stock_code: str, trade_date: str, days: int = 6) -> dict:
    """近 N 日 MA5/MA10/MA20 用於斜率計算（降序）。"""
    td = _date.fromisoformat(trade_date)
    rows = await fetch_all("""
        SELECT sma_5::float AS s5, sma_10::float AS s10, sma_20::float AS s20
        FROM stock_indicators
        WHERE stock_code = $1 AND trade_date <= $2
          AND sma_5 IS NOT NULL
        ORDER BY trade_date DESC LIMIT $3
    """, stock_code, td, days)
    return rows  # 列表，index 0 = 最新


async def _get_market_return(trade_date: str) -> float:
    """取大盤（加權指數，code=Y9999 或 market_indicators）當日漲幅。"""
    td = _date.fromisoformat(trade_date)
    row = await fetch_one("""
        SELECT taiex_close::float AS c FROM market_indicators
        WHERE trade_date = $1
    """, td)
    if row and row["c"]:
        prev = await fetch_one("""
            SELECT taiex_close::float AS c FROM market_indicators
            WHERE trade_date < $1 ORDER BY trade_date DESC LIMIT 1
        """, td)
        if prev and prev["c"] and prev["c"] != 0:
            return (row["c"] - prev["c"]) / prev["c"]

    # fallback: 用 0050 ETF 當大盤代理
    row2 = await fetch_one("""
        SELECT p.close_price::float AS c FROM stock_prices p
        WHERE p.stock_code = '0050' AND p.trade_date = $1
    """, td)
    prev2 = await fetch_one("""
        SELECT p.close_price::float AS c FROM stock_prices p
        WHERE p.stock_code = '0050' AND p.trade_date < $1
        ORDER BY p.trade_date DESC LIMIT 1
    """, td)
    if row2 and prev2 and prev2["c"]:
        return (row2["c"] - prev2["c"]) / prev2["c"]
    return 0.0


# ── 7 道過濾器 ───────────────────────────────────────────────────────────────

def _step1_return_filter(stocks: list[dict], prev_closes: dict) -> list[dict]:
    """Step 1: 漲幅過濾 2% ~ 9%。"""
    result = []
    for s in stocks:
        prev = prev_closes.get(s["stock_code"])
        if not prev or prev == 0:
            continue
        ret = (s["close_price"] - prev) / prev
        if CFG["return_min"] <= ret <= CFG["return_max"]:
            s["daily_return"] = round(ret, 4)
            result.append(s)
    logger.info(f"[Step1 漲幅] {len(stocks)} → {len(result)}")
    return result


def _step2_volume_ratio_filter(stocks: list[dict], vol_history: dict) -> list[dict]:
    """Step 2: 量比過濾（今日量/5日均量）。"""
    result = []
    for s in stocks:
        hist = vol_history.get(s["stock_code"], [])
        if len(hist) < 6:
            continue
        today_vol = hist[0]
        avg5 = sum(hist[1:6]) / 5
        if avg5 == 0:
            continue
        ratio = today_vol / avg5
        if ratio >= CFG["volume_ratio_min"]:
            s["volume_ratio"] = round(ratio, 2)
            s["avg5_volume"]   = avg5
            result.append(s)
    logger.info(f"[Step2 量比] → {len(result)}")
    return result


def _step3_turnover_filter(stocks: list[dict]) -> list[dict]:
    """Step 3: 換手率過濾 = 今日量(張) / 流通股(張)。"""
    result = []
    for s in stocks:
        float_sh = s.get("float_shares") or s.get("shares_outstanding")
        if not float_sh or float_sh == 0:
            # 無股本資料：暫時放行（不因缺資料排除）
            s["turnover_rate"] = None
            result.append(s)
            continue
        turnover = s["volume"] / float_sh
        if CFG["turnover_min"] <= turnover <= CFG["turnover_max"]:
            s["turnover_rate"] = round(turnover, 4)
            result.append(s)
    logger.info(f"[Step3 換手率] → {len(result)}")
    return result


def _step4_mktcap_filter(stocks: list[dict]) -> list[dict]:
    """Step 4: 流通市值過濾 250億~3000億 TWD。"""
    result = []
    for s in stocks:
        shares = s.get("float_shares") or s.get("shares_outstanding")
        if not shares:
            s["market_cap"] = None
            result.append(s)   # 無資料暫時放行
            continue
        # shares 單位：張（1張=1000股）
        mktcap = s["close_price"] * shares * 1000
        if CFG["mktcap_min"] <= mktcap <= CFG["mktcap_max"]:
            s["market_cap"] = round(mktcap / 1e8, 1)  # 億元
            result.append(s)
    logger.info(f"[Step4 市值] → {len(result)}")
    return result


def _step5_volume_staircase_filter(stocks: list[dict], vol_history: dict) -> list[dict]:
    """Step 5: 台階式放量（近3日量遞增，允許±10%誤差）。"""
    tol = CFG["vol_slope_tolerance"]
    result = []
    for s in stocks:
        hist = vol_history.get(s["stock_code"], [])
        if len(hist) < 3:
            continue
        v0, v1, v2 = hist[0], hist[1], hist[2]  # 今, 昨, 前天
        if v1 == 0 or v2 == 0:
            continue
        # 允許 ±tol 誤差：v0 > v1*(1-tol) 且 v1 > v2*(1-tol)
        if v0 >= v1 * (1 - tol) and v1 >= v2 * (1 - tol) and v0 > v2:
            s["vol_staircase"] = True
            result.append(s)
    logger.info(f"[Step5 台階量] → {len(result)}")
    return result


def _step6_ma_alignment_filter(stocks: list[dict], sma_hist: dict) -> list[dict]:
    """Step 6: 均線多頭排列 + MA5/10/20 斜率 > 0。"""
    result = []
    for s in stocks:
        ma5  = s.get("sma_5")
        ma10 = s.get("sma_10")
        ma20 = s.get("sma_20")
        ma60 = s.get("sma_60")
        price = s["close_price"]

        if None in (ma5, ma10, ma20, ma60):
            continue

        # 多頭排列
        if not (price > ma5 > ma10 > ma20 > ma60):
            continue

        # 斜率：(今日MA - 5日前MA) / 5日前MA > 0
        hist = sma_hist.get(s["stock_code"], [])
        if len(hist) < 5:
            continue
        old = hist[-1]  # 5日前
        slopes_ok = (
            old.get("s5")  and ma5  > old["s5"]  and
            old.get("s10") and ma10 > old["s10"] and
            old.get("s20") and ma20 > old["s20"]
        )
        if slopes_ok:
            s["ma_slope_5"]  = round((ma5  - old["s5"])  / old["s5"]  * 100, 3)
            s["ma_slope_10"] = round((ma10 - old["s10"]) / old["s10"] * 100, 3)
            s["ma_slope_20"] = round((ma20 - old["s20"]) / old["s20"] * 100, 3)
            result.append(s)
    logger.info(f"[Step6 均線] → {len(result)}")
    return result


def _step7_relative_strength_filter(stocks: list[dict], market_return: float) -> list[dict]:
    """Step 7: 相對強度 = 個股漲幅 - 大盤漲幅 > 0。"""
    result = []
    for s in stocks:
        rs = s.get("daily_return", 0) - market_return
        if rs > 0:
            s["relative_strength"] = round(rs, 4)
            result.append(s)
    logger.info(f"[Step7 相對強度] → {len(result)}")
    return result


def _bonus_filters(stocks: list[dict]) -> list[dict]:
    """加分項過濾：RSI<75 且 MACD柱狀>0。"""
    result = []
    for s in stocks:
        rsi  = s.get("rsi_14")
        macd = s.get("macd_histogram")
        rsi_ok  = rsi  is None or rsi  < CFG["rsi_max"]
        macd_ok = macd is None or macd > 0
        if rsi_ok and macd_ok:
            result.append(s)
    logger.info(f"[加分項 RSI+MACD] → {len(result)}")
    return result


# ── 綜合評分與排序 ─────────────────────────────────────────────────────────────

def _composite_score(s: dict) -> float:
    """
    綜合評分：量比(40%) + 相對強度(35%) + 換手率適配度(25%)。
    各項先歸一化再加權。
    """
    vr  = min(s.get("volume_ratio", 1) / 5.0, 1.0)       # 量比：上限5倍歸一
    rs  = min(s.get("relative_strength", 0) / 0.05, 1.0)  # 相對強度：5%歸一
    tr  = s.get("turnover_rate")
    # 換手率適配度：5%~8% 最佳，兩端遞減
    if tr is None:
        tr_score = 0.5
    elif 0.05 <= tr <= 0.08:
        tr_score = 1.0
    elif tr < 0.05:
        tr_score = tr / 0.05
    else:
        tr_score = max(0, 1 - (tr - 0.08) / 0.04)

    return round(vr * 0.40 + rs * 0.35 + tr_score * 0.25, 4)


# ── 主函數 ────────────────────────────────────────────────────────────────────

async def run_momentum_screening(trade_date: str) -> list[dict]:
    """
    執行尾盤動量突破篩選，回傳最終候選清單。
    結果同時寫入 momentum_candidates 表。
    """
    logger.info(f"[Momentum] 開始篩選 {trade_date}")

    # 取基礎宇宙
    universe = await _get_stock_universe(trade_date)
    if not universe:
        logger.warning("[Momentum] 無資料，可能今日休市")
        return []

    # 批次取前置資料
    codes = [s["stock_code"] for s in universe]
    prev_closes = {}
    vol_history = {}
    sma_hist    = {}
    for code in codes:
        prev_closes[code] = await _get_prev_close(code, trade_date)
        vol_history[code] = await _get_volume_history(code, trade_date, 6)
        sma_hist[code]    = await _get_sma_history(code, trade_date, 5)

    market_ret = await _get_market_return(trade_date)
    logger.info(f"[Momentum] 大盤漲幅：{market_ret*100:.2f}%，候選母體：{len(universe)} 檔")

    # ── 7 道過濾器 ────────────────────────────────────────────────────────────
    pool = universe
    pool = _step1_return_filter(pool, prev_closes)
    if not pool:
        return []
    pool = _step2_volume_ratio_filter(pool, vol_history)
    if not pool:
        return []
    pool = _step3_turnover_filter(pool)
    pool = _step4_mktcap_filter(pool)
    if not pool:
        return []
    pool = _step5_volume_staircase_filter(pool, vol_history)
    if not pool:
        return []
    pool = _step6_ma_alignment_filter(pool, sma_hist)
    if not pool:
        return []
    pool = _step7_relative_strength_filter(pool, market_ret)
    pool = _bonus_filters(pool)

    # ── 評分 + 排序 ───────────────────────────────────────────────────────────
    for s in pool:
        s["momentum_score"] = _composite_score(s)
    pool.sort(key=lambda x: x["momentum_score"], reverse=True)
    final = pool[:CFG["top_n"]]

    # ── 寫入 DB ───────────────────────────────────────────────────────────────
    td = _date.fromisoformat(trade_date)
    # 先清除當日舊紀錄
    await execute("DELETE FROM momentum_candidates WHERE screen_date = $1", td)

    for rank, s in enumerate(final, 1):
        await execute("""
            INSERT INTO momentum_candidates
                (screen_date, rank, stock_code, stock_name, sector,
                 daily_return, volume_ratio, turnover_rate, market_cap_bn,
                 relative_strength, momentum_score,
                 rsi_14, macd_histogram,
                 ma_slope_5, ma_slope_10, ma_slope_20)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
            ON CONFLICT (screen_date, stock_code) DO UPDATE SET
                rank             = EXCLUDED.rank,
                momentum_score   = EXCLUDED.momentum_score,
                daily_return     = EXCLUDED.daily_return,
                volume_ratio     = EXCLUDED.volume_ratio,
                updated_at       = NOW()
        """,
        td, rank,
        s["stock_code"], s["stock_name"], s.get("sector"),
        s.get("daily_return"), s.get("volume_ratio"),
        s.get("turnover_rate"), s.get("market_cap"),
        s.get("relative_strength"), s.get("momentum_score"),
        s.get("rsi_14"), s.get("macd_histogram"),
        s.get("ma_slope_5"), s.get("ma_slope_10"), s.get("ma_slope_20"))

    logger.info(f"[Momentum] 篩選完成，最終 {len(final)} 檔")
    return final


async def calculate_momentum_returns(screen_date: str):
    """
    次日收盤後回算報酬率，驗證策略有效性。
    傳入的 screen_date 是篩選日（訊號發生日），計算 +1/+3/+5 日報酬。
    """
    td = _date.fromisoformat(screen_date)
    candidates = await fetch_all("""
        SELECT stock_code, screen_date FROM momentum_candidates
        WHERE screen_date = $1 AND return_1d IS NULL
    """, td)

    for c in candidates:
        code = c["stock_code"]
        # 取訊號日收盤 + 後續報價
        prices = await fetch_all("""
            SELECT trade_date, close_price::float AS c
            FROM stock_prices
            WHERE stock_code = $1 AND trade_date >= $2
            ORDER BY trade_date ASC LIMIT 6
        """, code, td)

        if len(prices) < 2:
            continue
        entry = prices[0]["c"]
        if entry == 0:
            continue

        def ret(n):
            return round((prices[n]["c"] - entry) / entry * 100, 2) if len(prices) > n else None

        await execute("""
            UPDATE momentum_candidates SET
                return_1d = $3,
                return_3d = $4,
                return_5d = $5,
                updated_at = NOW()
            WHERE stock_code = $1 AND screen_date = $2
        """, code, td, ret(1), ret(3) if len(prices) > 3 else None,
             ret(5) if len(prices) > 5 else None)

    logger.info(f"[Momentum] 報酬率回算完成：{screen_date}")

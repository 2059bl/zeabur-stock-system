"""
8 維外資離場空頭信號引擎

維度：
  D1 外資現貨連賣天數       → 讀 stock-ai-agent DB stock_indicators
  D2 投信現貨買賣超趨勢      → 同上
  D3 台指期外資淨空單        → FinMind TaiwanFuturesInstitutionalInvestors
  D4 USD/TWD 台幣貶值幅度   → FinMind TaiwanExchangeRate
  D5 融資餘額月變化          → FinMind TaiwanStockMarginPurchaseShortSale
  D6 融券餘額（空頭籌碼）    → 同上
  D7 大盤近月走勢            → 讀 stock-ai-agent DB stock_prices (0050)
  D8 產業輪動（IC screener） → 讀 ic-screener DB screener_results
"""
import asyncio
import logging
import datetime
from utils.db          import fetch_all, fetch_one, execute
from utils.market_data import fetch_futures_institutional, fetch_usdtwd, fetch_margin_aggregate

logger = logging.getLogger(__name__)

# 信號等級門檻
LEVEL_MAP = [
    (75, "EXTREME", "🔴 極度危險：立即減碼至防禦部位"),
    (55, "DANGER",  "🟠 危險：分批減碼 30-50%，建立對沖"),
    (35, "WARNING", "🟡 警示：縮短持股週期，備好停損"),
    (0,  "NORMAL",  "🟢 正常：維持策略，持續監控"),
]

# 追蹤股票清單（從 ic-screener 常見股取樣）
SAMPLE_STOCKS = [
    "2330","2454","2317","2382","6669","3711","2308","2303",
    "3034","2379","2449","3231","4938","2356","2357","2353",
    "3017","5274","6239","3264","2327","2492","3037","8046",
]


def _level(score: float) -> tuple[str, str]:
    for threshold, level, action in LEVEL_MAP:
        if score >= threshold:
            return level, action
    return "NORMAL", "🟢 正常：維持策略"


async def _d1_d2_foreign_trust() -> dict:
    """D1/D2：讀 stock-ai-agent DB 外資/投信近期買賣超。"""
    try:
        rows = await fetch_all("""
            SELECT trade_date,
                   SUM(foreign_net_buy) AS total_foreign,
                   SUM(investment_trust_net_buy) AS total_trust
            FROM stock_indicators
            WHERE trade_date >= CURRENT_DATE - INTERVAL '10 days'
              AND foreign_net_buy IS NOT NULL
            GROUP BY trade_date
            ORDER BY trade_date DESC
            LIMIT 10
        """)
    except Exception:
        rows = []

    if not rows:
        return {"foreign_sell_days": 0, "trust_sell_days": 0,
                "foreign_cum_5d": 0, "d1_score": 0, "d2_score": 0}

    f_sell_days = sum(1 for r in rows if (r.get("total_foreign") or 0) < 0)
    t_sell_days = sum(1 for r in rows if (r.get("total_trust") or 0) < 0)
    f_cum_5d    = sum((r.get("total_foreign") or 0) for r in rows[:5])

    d1 = min(100, f_sell_days * 10 + abs(f_cum_5d) / 1000)
    d2 = min(100, t_sell_days * 8)
    return {
        "foreign_sell_days": f_sell_days,
        "trust_sell_days":   t_sell_days,
        "foreign_cum_5d":    int(f_cum_5d),
        "d1_score": round(d1, 1),
        "d2_score": round(d2, 1),
    }


async def _d7_index_trend() -> dict:
    """D7：讀 0050 近月股價走勢作為大盤代理指標。"""
    try:
        rows = await fetch_all("""
            SELECT trade_date, close_price
            FROM stock_prices
            WHERE stock_code = '0050'
            ORDER BY trade_date DESC
            LIMIT 22
        """)
    except Exception:
        rows = []

    if len(rows) < 5:
        return {"index_m1_pct": None, "d7_score": 0}

    latest = float(rows[0]["close_price"])
    month_ago = float(rows[-1]["close_price"])
    m1_pct = round((latest - month_ago) / month_ago * 100, 2) if month_ago else 0

    # 大盤下跌 → 加空頭分數
    if m1_pct <= -10:   d7 = 80
    elif m1_pct <= -5:  d7 = 50
    elif m1_pct <= -2:  d7 = 25
    else:               d7 = 0

    return {"index_m1_pct": m1_pct, "d7_score": d7}


async def _d8_industry_rotation() -> dict:
    """
    D8：產業輪動警示。
    讀 ic-screener screener_results，對比最新兩次篩選結果，
    判斷哪些池入選數下滑。
    """
    try:
        # 取最近兩個篩選日期
        dates = await fetch_all("""
            SELECT DISTINCT screen_date FROM screener_results
            ORDER BY screen_date DESC LIMIT 2
        """)
    except Exception:
        return {"weakening_pools": [], "d8_score": 0}

    if len(dates) < 2:
        return {"weakening_pools": [], "d8_score": 0}

    latest_dt = dates[0]["screen_date"]
    prev_dt   = dates[1]["screen_date"]

    try:
        latest_counts = await fetch_all("""
            SELECT p.pool_name, COUNT(*) as cnt
            FROM screener_results r
            JOIN screener_pools p ON p.pool_id = r.pool_id
            WHERE r.screen_date = $1
            GROUP BY p.pool_name
        """, latest_dt)
        prev_counts = await fetch_all("""
            SELECT p.pool_name, COUNT(*) as cnt
            FROM screener_results r
            JOIN screener_pools p ON p.pool_id = r.pool_id
            WHERE r.screen_date = $1
            GROUP BY p.pool_name
        """, prev_dt)
    except Exception:
        return {"weakening_pools": [], "d8_score": 0}

    prev_map   = {r["pool_name"]: r["cnt"] for r in prev_counts}
    latest_map = {r["pool_name"]: r["cnt"] for r in latest_counts}

    weakening = []
    for pool, prev_cnt in prev_map.items():
        now_cnt = latest_map.get(pool, 0)
        if prev_cnt > 0 and now_cnt == 0:
            weakening.append(f"{pool}（{prev_cnt}→0）")
        elif prev_cnt >= 3 and now_cnt < prev_cnt // 2:
            weakening.append(f"{pool}（{prev_cnt}→{now_cnt} ↓）")

    d8 = min(100, len(weakening) * 20)
    return {
        "weakening_pools": weakening,
        "latest_date":     str(latest_dt),
        "prev_date":       str(prev_dt),
        "d8_score":        d8,
    }


async def compute_bear_signal() -> dict:
    """計算完整 8 維空頭信號，回傳評分與等級。"""
    today = str(datetime.date.today())

    # 並行抓取所有資料
    (inst_db, futures, usdtwd, margin, index_t, rotation) = await asyncio.gather(
        _d1_d2_foreign_trust(),
        fetch_futures_institutional(),
        fetch_usdtwd(),
        fetch_margin_aggregate(SAMPLE_STOCKS),
        _d7_index_trend(),
        _d8_industry_rotation(),
    )

    # D3：台指期淨空單評分
    net_short = futures.get("net_short", 0)
    trend_5d  = futures.get("trend_5d", 0)
    if net_short >= 80000:    d3 = 90
    elif net_short >= 50000:  d3 = 65
    elif net_short >= 30000:  d3 = 40
    elif net_short >= 10000:  d3 = 20
    else:                     d3 = 0
    if trend_5d > 5000:       d3 = min(100, d3 + 15)  # 空單持續增加

    # D4：台幣貶值評分
    deprec = usdtwd.get("deprec_pct_1m", 0)
    if deprec >= 5:     d4 = 80
    elif deprec >= 3:   d4 = 50
    elif deprec >= 1.5: d4 = 25
    elif deprec >= 0:   d4 = 5
    else:               d4 = 0   # 台幣升值 = 外資回流

    # D5：融資月變化評分（負=融資減少=空頭信號）
    margin_chg = margin.get("margin_chg_pct", 0)
    if margin_chg <= -15:   d5 = 70
    elif margin_chg <= -8:  d5 = 45
    elif margin_chg <= -3:  d5 = 20
    else:                   d5 = 0

    # D6：融券餘額（越高越空）
    short_bal = margin.get("short_balance", 0)
    margin_bal = margin.get("margin_today", 1)
    short_ratio = round(short_bal / margin_bal * 100, 1) if margin_bal else 0
    if short_ratio >= 15:   d6 = 70
    elif short_ratio >= 8:  d6 = 40
    elif short_ratio >= 4:  d6 = 20
    else:                   d6 = 0

    scores = {
        "D1_外資現貨":   inst_db["d1_score"],
        "D2_投信現貨":   inst_db["d2_score"],
        "D3_台指期空單": round(d3, 1),
        "D4_台幣貶值":   round(d4, 1),
        "D5_融資減少":   round(d5, 1),
        "D6_融券增加":   round(d6, 1),
        "D7_大盤走勢":   index_t["d7_score"],
        "D8_產業輪動":   rotation["d8_score"],
    }
    total = round(sum(scores.values()) / len(scores), 1)
    level, action = _level(total)

    result = {
        "date":    today,
        "score":   total,
        "level":   level,
        "action":  action,
        "scores":  scores,
        # 原始數據
        "foreign_sell_days":  inst_db["foreign_sell_days"],
        "trust_sell_days":    inst_db["trust_sell_days"],
        "foreign_cum_5d":     inst_db["foreign_cum_5d"],
        "futures_net_short":  net_short,
        "futures_trend_5d":   futures.get("trend_5d", 0),
        "usdtwd_rate":        usdtwd.get("rate"),
        "usdtwd_deprec_pct":  deprec,
        "margin_chg_pct":     margin_chg,
        "short_ratio":        short_ratio,
        "index_m1_pct":       index_t.get("index_m1_pct"),
        "weakening_pools":    rotation["weakening_pools"],
        "rotation_latest_dt": rotation.get("latest_date"),
        "rotation_prev_dt":   rotation.get("prev_date"),
    }

    logger.info(f"Bear Signal {today}: score={total} level={level} "
                f"futures_net_short={net_short:,} deprec={deprec}%")
    return result


async def save_signal(result: dict):
    """將信號結果寫入 bear_market_indicators 表。"""
    await execute("""
        INSERT INTO bear_market_indicators
            (signal_date, total_score, signal_level,
             d1_foreign, d2_trust, d3_futures, d4_currency,
             d5_margin, d6_short, d7_index, d8_rotation,
             foreign_sell_days, futures_net_short,
             usdtwd_rate, usdtwd_deprec_pct,
             margin_chg_pct, short_ratio, index_m1_pct,
             weakening_pools, action_text)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
        ON CONFLICT (signal_date) DO UPDATE SET
            total_score      = EXCLUDED.total_score,
            signal_level     = EXCLUDED.signal_level,
            d3_futures       = EXCLUDED.d3_futures,
            d4_currency      = EXCLUDED.d4_currency,
            futures_net_short = EXCLUDED.futures_net_short,
            usdtwd_rate      = EXCLUDED.usdtwd_rate,
            weakening_pools  = EXCLUDED.weakening_pools,
            updated_at       = NOW()
    """,
        result["date"], result["score"], result["level"],
        result["scores"]["D1_外資現貨"], result["scores"]["D2_投信現貨"],
        result["scores"]["D3_台指期空單"], result["scores"]["D4_台幣貶值"],
        result["scores"]["D5_融資減少"], result["scores"]["D6_融券增加"],
        result["scores"]["D7_大盤走勢"], result["scores"]["D8_產業輪動"],
        result["foreign_sell_days"], result["futures_net_short"],
        result["usdtwd_rate"], result["usdtwd_deprec_pct"],
        result["margin_chg_pct"], result["short_ratio"],
        result["index_m1_pct"],
        ",".join(result["weakening_pools"]) if result["weakening_pools"] else None,
        result["action"],
    )

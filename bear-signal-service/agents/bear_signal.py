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
from utils.market_data import fetch_futures_institutional, fetch_usdtwd, fetch_margin_aggregate, _get
from utils.news_scanner import scan_news

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


_D1D2_BASKET = ["2330", "2454", "2317", "2382", "6669"]  # 五大權值股 basket


async def _d1_d2_foreign_trust() -> dict:
    """
    D1/D2：聚合五大權值股（basket）外資/投信每日淨買賣，
    推估市場整體外資方向。欄位：name（Foreign_Investor / Investment_Trust）。
    """
    try:
        tasks = [_get("TaiwanStockInstitutionalInvestorsBuySell", c, days=14)
                 for c in _D1D2_BASKET]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        all_rows = []
        for r in results:
            if isinstance(r, list):
                all_rows.extend(r)
    except Exception:
        all_rows = []

    # 按日期 + 名稱彙總 basket 總買賣
    from collections import defaultdict
    day_foreign: dict[str, dict] = defaultdict(lambda: {"buy": 0, "sell": 0})
    day_trust:   dict[str, dict] = defaultdict(lambda: {"buy": 0, "sell": 0})

    for row in all_rows:
        dt   = row.get("date", "")
        name = row.get("name", "")
        buy  = row.get("buy") or 0
        sell = row.get("sell") or 0
        if name == "Foreign_Investor":
            day_foreign[dt]["buy"]  += buy
            day_foreign[dt]["sell"] += sell
        elif name == "Investment_Trust":
            day_trust[dt]["buy"]  += buy
            day_trust[dt]["sell"] += sell

    if not day_foreign:
        return {"foreign_sell_days": 0, "trust_sell_days": 0,
                "foreign_cum_5d": 0, "d1_score": 0, "d2_score": 0}

    sorted_dates = sorted(day_foreign.keys(), reverse=True)

    f_sell_days = sum(
        1 for dt in sorted_dates[:10]
        if day_foreign[dt]["buy"] < day_foreign[dt]["sell"]
    )
    t_sell_days = sum(
        1 for dt in sorted(day_trust.keys(), reverse=True)[:10]
        if day_trust[dt]["buy"] < day_trust[dt]["sell"]
    )
    f_cum_5d = sum(
        day_foreign[dt]["buy"] - day_foreign[dt]["sell"]
        for dt in sorted_dates[:5]
    )

    # 每億元淨賣出加 1 分，連賣天數每天加 10 分
    d1 = min(100, f_sell_days * 10 + abs(f_cum_5d) / 1e8 * 10)
    d2 = min(100, t_sell_days * 8)

    logger.info(f"[D1/D2] basket={_D1D2_BASKET} f_sell_days={f_sell_days} "
                f"f_cum_5d={f_cum_5d:+,.0f} d1={d1} d2={d2}")
    return {
        "foreign_sell_days": f_sell_days,
        "trust_sell_days":   t_sell_days,
        "foreign_cum_5d":    int(f_cum_5d),
        "d1_score": round(d1, 1),
        "d2_score": round(d2, 1),
    }


async def _d7_index_trend() -> dict:
    """D7：FinMind TaiwanStockPrice 0050 近月走勢作為大盤代理指標。"""
    try:
        rows = await _get("TaiwanStockPrice", "0050", days=35)
        sorted_rows = sorted(rows, key=lambda x: x["date"])
    except Exception:
        sorted_rows = []

    if len(sorted_rows) < 5:
        return {"index_m1_pct": None, "d7_score": 0}

    latest   = float(sorted_rows[-1].get("close", 0) or 0)
    month_ago = float(sorted_rows[0].get("close", 0) or 0)
    m1_pct = round((latest - month_ago) / month_ago * 100, 2) if month_ago else 0

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
    (inst_db, futures, usdtwd, margin, index_t, rotation, news) = await asyncio.gather(
        _d1_d2_foreign_trust(),
        fetch_futures_institutional(),
        fetch_usdtwd(),
        fetch_margin_aggregate(SAMPLE_STOCKS),
        _d7_index_trend(),
        _d8_industry_rotation(),
        scan_news(),
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
        "D9_新聞情緒":   news["d9_score"],
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
        # D9 新聞情緒
        "news_d9_score":      news["d9_score"],
        "news_article_count": news.get("article_count", 0),
        "news_black_swan":    news.get("black_swan_hits", []),
        "news_gray_rhino":    news.get("gray_rhino_hits", []),
        "news_sentiment":     news.get("sentiment_score", 0),
        "news_risk_level":    news.get("llm_risk_level", "LOW"),
        "news_key_risks":     news.get("key_risks", []),
        "news_summary":       news.get("summary", ""),
    }

    logger.info(f"Bear Signal {today}: score={total} level={level} "
                f"futures_net_short={net_short:,} deprec={deprec}%")
    return result


async def save_signal(result: dict):
    """將信號結果寫入 bear_market_indicators 表。"""
    sig_date = (datetime.date.fromisoformat(result["date"])
                if isinstance(result["date"], str) else result["date"])
    black_swan_text = "; ".join(result.get("news_black_swan", []))[:500] or None
    await execute("""
        INSERT INTO bear_market_indicators
            (signal_date, total_score, signal_level,
             d1_foreign, d2_trust, d3_futures, d4_currency,
             d5_margin, d6_short, d7_index, d8_rotation, d9_news,
             foreign_sell_days, futures_net_short,
             usdtwd_rate, usdtwd_deprec_pct,
             margin_chg_pct, short_ratio, index_m1_pct,
             weakening_pools, news_summary, news_risk_level, news_black_swan,
             action_text)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24)
        ON CONFLICT (signal_date) DO UPDATE SET
            total_score       = EXCLUDED.total_score,
            signal_level      = EXCLUDED.signal_level,
            d1_foreign        = EXCLUDED.d1_foreign,
            d2_trust          = EXCLUDED.d2_trust,
            d3_futures        = EXCLUDED.d3_futures,
            d4_currency       = EXCLUDED.d4_currency,
            d5_margin         = EXCLUDED.d5_margin,
            d6_short          = EXCLUDED.d6_short,
            d7_index          = EXCLUDED.d7_index,
            d8_rotation       = EXCLUDED.d8_rotation,
            d9_news           = EXCLUDED.d9_news,
            foreign_sell_days = EXCLUDED.foreign_sell_days,
            futures_net_short = EXCLUDED.futures_net_short,
            usdtwd_rate       = EXCLUDED.usdtwd_rate,
            usdtwd_deprec_pct = EXCLUDED.usdtwd_deprec_pct,
            margin_chg_pct    = EXCLUDED.margin_chg_pct,
            short_ratio       = EXCLUDED.short_ratio,
            index_m1_pct      = EXCLUDED.index_m1_pct,
            weakening_pools   = EXCLUDED.weakening_pools,
            news_summary      = EXCLUDED.news_summary,
            news_risk_level   = EXCLUDED.news_risk_level,
            news_black_swan   = EXCLUDED.news_black_swan,
            action_text       = EXCLUDED.action_text,
            updated_at        = NOW()
    """,
        sig_date, result["score"], result["level"],
        result["scores"]["D1_外資現貨"], result["scores"]["D2_投信現貨"],
        result["scores"]["D3_台指期空單"], result["scores"]["D4_台幣貶值"],
        result["scores"]["D5_融資減少"], result["scores"]["D6_融券增加"],
        result["scores"]["D7_大盤走勢"], result["scores"]["D8_產業輪動"],
        result["scores"]["D9_新聞情緒"],
        result["foreign_sell_days"], result["futures_net_short"],
        result["usdtwd_rate"], result["usdtwd_deprec_pct"],
        result["margin_chg_pct"], result["short_ratio"],
        result["index_m1_pct"],
        ",".join(result["weakening_pools"]) if result["weakening_pools"] else None,
        result.get("news_summary"),
        result.get("news_risk_level"),
        black_swan_text,
        result["action"],
    )

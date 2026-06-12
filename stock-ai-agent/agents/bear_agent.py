from utils.db import fetch_all, execute


async def detect_bull_trap(stock_code: str, trade_date: str) -> str:
    rows = await fetch_all("""
        SELECT i.rsi_14, p.change_pct
        FROM stock_indicators i
        JOIN stock_prices p ON p.stock_code = i.stock_code AND p.trade_date = i.trade_date
        WHERE i.stock_code = $1 AND i.trade_date = $2
    """, stock_code, trade_date)

    if not rows:
        return "NORMAL"
    rsi = rows[0]["rsi_14"] or 50
    chg = rows[0]["change_pct"] or 0
    if rsi > 65 and chg < -1.5:
        return "BULL_TRAP"
    return "NORMAL"


async def run_dynamic_screening(trade_date: str) -> list[dict]:
    active_stocks = await fetch_all(
        "SELECT stock_code FROM stocks WHERE is_active = TRUE"
    )
    candidates = []

    for s in active_stocks:
        code = s["stock_code"]
        trap_status = await detect_bull_trap(code, trade_date)

        await execute("""
            UPDATE stock_indicators
            SET bad_news_test_status = $1
            WHERE stock_code = $2 AND trade_date = $3
        """, trap_status, code, trade_date)

        ind = await fetch_all(
            "SELECT * FROM latest_indicators WHERE stock_code = $1", code
        )
        if ind and ind[0].get("is_bear_alignment"):
            candidates.append({
                "stock_code": code,
                "stock_name": ind[0].get("stock_name", ""),
                "sector": ind[0].get("sector", ""),
                "news_test": trap_status,
                "bear_signal_score": ind[0].get("bear_signal_score"),
                "institution_flow_signal": ind[0].get("institution_flow_signal"),
            })

    return candidates

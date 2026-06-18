"""
停損預警引擎
- 讀取 ic-screener screener_results（有 close_price 欄位）
- 比對當前最新股價（stock-ai-agent stock_prices 表）
- 跌幅超過門檻 → 推播 Telegram
- 每日 14:30 排程（盤中最後半小時，有時間反應）
"""
import logging
import datetime
from utils.db       import fetch_all
from utils.notifier import send
from utils.market_data import _get as finmind_get

logger = logging.getLogger(__name__)

# 停損門檻設定
STOP_LOSS_PCT   = -8.0    # 跌幅 > 8% 觸發
TRAILING_DAYS   = 30      # 只監控最近 30 天選出的股票
QUICK_DROP_PCT  = -5.0    # 近 3 日快速下跌也預警（黑天鵝保護）
QUICK_DROP_DAYS = 3


async def _get_screened_positions() -> list[dict]:
    """
    取得近 N 天篩選出的股票及當時入場價。
    """
    cutoff = datetime.date.today() - datetime.timedelta(days=TRAILING_DAYS)
    rows = await fetch_all("""
        SELECT DISTINCT ON (r.stock_code)
               r.stock_code,
               s.stock_name,
               r.screen_date,
               r.close_price  AS entry_price,
               p.pool_name
        FROM screener_results r
        JOIN screener_stocks ss ON ss.stock_code = r.stock_code
        JOIN screener_stocks s  ON s.stock_code  = r.stock_code
        JOIN screener_pools  p  ON p.pool_id     = r.pool_id
        WHERE r.screen_date >= $1
          AND r.close_price IS NOT NULL
          AND r.close_price > 0
        ORDER BY r.stock_code, r.screen_date DESC
    """, cutoff)
    return rows


async def _get_latest_prices(stock_codes: list[str]) -> dict[str, float]:
    """
    從 stock-ai-agent stock_prices 取最新收盤價。
    若 DB 中無資料（非追蹤股），改從 FinMind 抓。
    """
    if not stock_codes:
        return {}

    # 先查 DB
    placeholders = ",".join(f"${i+1}" for i in range(len(stock_codes)))
    db_rows = await fetch_all(f"""
        SELECT DISTINCT ON (stock_code)
               stock_code, close_price
        FROM stock_prices
        WHERE stock_code = ANY(ARRAY[{placeholders}])
        ORDER BY stock_code, trade_date DESC
    """, *stock_codes)

    prices = {r["stock_code"]: float(r["close_price"]) for r in db_rows}

    # 不在 DB 的股票改用 FinMind
    missing = [c for c in stock_codes if c not in prices]
    if missing:
        import asyncio
        tasks = [finmind_get("TaiwanStockPrice", code, days=5) for code in missing[:10]]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for code, rows in zip(missing[:10], results):
            if isinstance(rows, list) and rows:
                latest = sorted(rows, key=lambda x: x["date"])[-1]
                prices[code] = float(latest.get("close", 0) or 0)

    return prices


async def _get_recent_prices_3d(stock_codes: list[str]) -> dict[str, list[float]]:
    """取近 3 日收盤價，用於快速下跌偵測。"""
    if not stock_codes:
        return {}
    cutoff = datetime.date.today() - datetime.timedelta(days=10)
    placeholders = ",".join(f"${i+2}" for i in range(len(stock_codes)))
    rows = await fetch_all(f"""
        SELECT stock_code, trade_date, close_price
        FROM stock_prices
        WHERE trade_date >= $1
          AND stock_code = ANY(ARRAY[{placeholders}])
        ORDER BY stock_code, trade_date DESC
    """, cutoff, *stock_codes)

    result: dict[str, list[float]] = {}
    for r in rows:
        c = r["stock_code"]
        result.setdefault(c, [])
        if len(result[c]) < QUICK_DROP_DAYS + 1:
            result[c].append(float(r["close_price"]))
    return result


async def check_stop_loss() -> dict:
    """
    主入口：計算所有持倉的停損狀態。
    回傳觸發預警的清單。
    """
    positions = await _get_screened_positions()
    if not positions:
        logger.info("[StopLoss] 無近期篩選持倉")
        return {"triggered": [], "checked": 0}

    codes = [p["stock_code"] for p in positions]
    latest_prices = await _get_latest_prices(codes)
    recent_prices = await _get_recent_prices_3d(codes)

    triggered = []
    for pos in positions:
        code        = pos["stock_code"]
        name        = pos.get("stock_name", code)
        entry_price = float(pos["entry_price"] or 0)
        current     = latest_prices.get(code)
        pool        = pos.get("pool_name", "?")
        screen_date = pos.get("screen_date")

        if not current or entry_price <= 0:
            continue

        drop_pct = (current - entry_price) / entry_price * 100

        alert_type = None
        if drop_pct <= STOP_LOSS_PCT:
            alert_type = "STOP_LOSS"
        else:
            # 快速下跌偵測
            prices_3d = recent_prices.get(code, [])
            if len(prices_3d) >= 2:
                quick_drop = (prices_3d[0] - prices_3d[-1]) / prices_3d[-1] * 100
                if quick_drop <= QUICK_DROP_PCT:
                    alert_type = "QUICK_DROP"

        if alert_type:
            triggered.append({
                "stock_code":  code,
                "stock_name":  name,
                "pool":        pool,
                "entry_price": entry_price,
                "current":     current,
                "drop_pct":    round(drop_pct, 2),
                "screen_date": str(screen_date),
                "alert_type":  alert_type,
            })
            logger.warning(f"[StopLoss] {alert_type} {code} {name} "
                           f"entry={entry_price} now={current} {drop_pct:.1f}%")

    logger.info(f"[StopLoss] 檢查 {len(positions)} 檔，觸發 {len(triggered)} 個預警")
    return {"triggered": triggered, "checked": len(positions)}


async def notify_stop_loss(result: dict):
    """推播停損預警到 Telegram。"""
    triggered = result.get("triggered", [])
    if not triggered:
        return

    lines = [
        f"🛑 *停損預警 {datetime.date.today()}*",
        f"共 {len(triggered)} 檔觸發，請立即檢視：",
        "",
    ]
    for t in triggered:
        emoji = "🔴" if t["alert_type"] == "STOP_LOSS" else "🟠"
        lines.append(
            f"{emoji} `{t['stock_code']}` {t['stock_name']}  [{t['pool']}]"
        )
        lines.append(
            f"   入場 {t['entry_price']} → 現價 {t['current']}  "
            f"({'停損' if t['alert_type'] == 'STOP_LOSS' else '快速下跌'} *{t['drop_pct']:+.1f}%*)"
        )
        lines.append(f"   篩選日：{t['screen_date']}")
        lines.append("")

    await send("\n".join(lines))

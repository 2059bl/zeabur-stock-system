"""
停損預警引擎
- 讀取 ic-screener screener_results（有 close_price 欄位）
- 比對當前最新股價（stock-ai-agent stock_prices 表）
- 跌幅超過門檻 → 推播 Telegram
- 每日 14:30 排程（盤中最後半小時，有時間反應）
"""
import logging
import datetime
import asyncio
from utils.db          import fetch_all
from utils.notifier    import send
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
        JOIN screener_stocks s ON s.stock_code = r.stock_code
        JOIN screener_pools  p ON p.pool_id    = r.pool_id
        WHERE r.screen_date >= $1
          AND r.close_price IS NOT NULL
          AND r.close_price > 0
        ORDER BY r.stock_code, r.screen_date DESC
    """, cutoff)
    return rows


async def _get_latest_and_recent_prices(stock_codes: list[str]) -> dict[str, dict]:
    """
    用 FinMind TaiwanStockPrice 取最新及近 5 日收盤價。
    限制並發 10 檔避免 rate limit。
    """
    if not stock_codes:
        return {}

    result: dict[str, dict] = {}
    batch = stock_codes[:15]
    tasks = [finmind_get("TaiwanStockPrice", code, days=10) for code in batch]
    responses = await asyncio.gather(*tasks, return_exceptions=True)

    for code, rows in zip(batch, responses):
        if isinstance(rows, Exception) or not rows:
            continue
        sorted_rows = sorted(rows, key=lambda x: x["date"])
        closes = [float(r.get("close", 0) or 0) for r in sorted_rows if r.get("close")]
        if closes:
            result[code] = {
                "latest":   closes[-1],
                "recent5":  closes[-5:],
            }
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
    price_data = await _get_latest_and_recent_prices(codes)

    triggered = []
    for pos in positions:
        code        = pos["stock_code"]
        name        = pos.get("stock_name", code)
        entry_price = float(pos["entry_price"] or 0)
        pdata       = price_data.get(code)
        pool        = pos.get("pool_name", "?")
        screen_date = pos.get("screen_date")

        if not pdata or entry_price <= 0:
            continue

        current  = pdata["latest"]
        recent5  = pdata["recent5"]
        drop_pct = (current - entry_price) / entry_price * 100

        alert_type = None
        if drop_pct <= STOP_LOSS_PCT:
            alert_type = "STOP_LOSS"
        elif len(recent5) >= 2:
            quick_drop = (recent5[-1] - recent5[0]) / recent5[0] * 100
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

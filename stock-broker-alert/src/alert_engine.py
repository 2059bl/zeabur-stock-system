"""
Alert Engine — 每日掃描 Watchlist，觸發三種警報
1. 異常大買（占成交量 >= 3% 或金額 > 5000萬）
2. 連續佈局（同分點同股票連續 3 日以上買超）
3. 隔日沖風險（day_trade_risk = HIGH 且今日有大量買超）
"""
import asyncio
import logging
from datetime import date, timedelta

from src.db import execute, fetch_all
from src.broker_score import calc_day_trade_score
from src.telegram import (
    fmt_alert_abnormal_buy, fmt_alert_consecutive,
    fmt_alert_day_trade_risk, send_alert,
)

logger = logging.getLogger(__name__)

# ─── 警報閾值 ─────────────────────────────────────────────────────────────────
ABS_RATIO_THRESHOLD = float(3.0)    # 占成交量 3% 觸發異常大買
ABS_AMOUNT_THRESHOLD = 50_000_000   # 5000 萬元觸發
CONSECUTIVE_DAYS    = 3              # 連續 N 日觸發
HIGH_DT_SCORE       = 60.0          # 隔日沖分數閾值


async def _get_watchlist_brokers() -> list[dict]:
    return await fetch_all(
        """SELECT broker_code, broker_name, broker_score, day_trade_risk,
                  day_trade_score, blood_selling_dates, detected_sectors
           FROM broker_watchlist WHERE active=TRUE ORDER BY broker_score DESC"""
    )


async def _get_today_actions(trade_date: str, broker_code: str) -> list[dict]:
    return await fetch_all(
        """SELECT bda.trade_date, bda.stock_code, bda.net_volume,
                  bda.absorption_ratio, bda.net_amount, bda.stock_volume,
                  bda.buy_volume, bda.sell_volume,
                  COALESCE(gs.stock_name, eu.etf_name, bda.stock_code) AS stock_name
           FROM broker_daily_actions bda
           LEFT JOIN growth_stock_universe gs ON bda.stock_code=gs.stock_code
           LEFT JOIN etf_universe eu          ON bda.stock_code=eu.etf_code
           WHERE bda.broker_code=$1 AND bda.trade_date=$2 AND bda.net_volume > 0
           ORDER BY bda.net_volume DESC""",
        broker_code, date.fromisoformat(trade_date),
    )


async def _get_consecutive_data(
    broker_code: str,
    stock_code: str,
    trade_date: str,
    days: int = 5,
) -> list[dict]:
    start = (date.fromisoformat(trade_date) - timedelta(days=days * 2)).isoformat()
    rows = await fetch_all(
        """SELECT trade_date, net_volume, net_amount
           FROM broker_daily_actions
           WHERE broker_code=$1 AND stock_code=$2 AND trade_date >= $3 AND net_volume > 0
           ORDER BY trade_date DESC""",
        broker_code, stock_code, date.fromisoformat(start),
    )
    # 找連續日期（可跨週末）
    if not rows:
        return []
    consec = [rows[0]]
    prev   = rows[0]["trade_date"]
    for r in rows[1:]:
        gap = abs((prev - r["trade_date"]).days)
        if gap <= 4:  # 允許週末
            consec.append(r)
            prev = r["trade_date"]
        else:
            break
    return consec


async def _already_alerted(trade_date: str, broker_code: str, stock_code: str, alert_type: str) -> bool:
    row = await fetch_all(
        "SELECT id FROM broker_alerts WHERE alert_date=$1 AND broker_code=$2 AND stock_code=$3 AND alert_type=$4",
        date.fromisoformat(trade_date), broker_code, stock_code, alert_type,
    )
    return len(row) > 0


async def _save_alert(
    trade_date: str,
    broker_code: str,
    broker_name: str,
    stock_code: str,
    stock_name: str,
    alert_type: str,
    alert_level: str,
    net_volume: int,
    absorption_ratio: float,
    net_amount: float,
    consecutive_days: int,
    broker_score: int,
    message: str,
) -> bool:
    try:
        await execute(
            """
            INSERT INTO broker_alerts
              (alert_date, broker_code, broker_name, stock_code, stock_name,
               alert_type, alert_level, net_volume, absorption_ratio, net_amount,
               consecutive_days, broker_score, message)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT(alert_date, broker_code, stock_code, alert_type) DO NOTHING
            """,
            date.fromisoformat(trade_date), broker_code, broker_name,
            stock_code, stock_name, alert_type, alert_level,
            net_volume, absorption_ratio, net_amount, consecutive_days, broker_score, message,
        )
        return True
    except Exception as e:
        logger.warning(f"alert save failed: {e}")
        return False


async def _mark_sent(trade_date: str, broker_code: str, stock_code: str, alert_type: str):
    try:
        await execute(
            """UPDATE broker_alerts SET telegram_sent=TRUE, telegram_sent_at=NOW()
               WHERE alert_date=$1 AND broker_code=$2 AND stock_code=$3 AND alert_type=$4""",
            date.fromisoformat(trade_date), broker_code, stock_code, alert_type,
        )
    except Exception:
        pass


async def run_daily_alerts(trade_date: str) -> int:
    """
    主警報掃描流程。
    Returns: 觸發的警報總數
    """
    logger.info(f"=== Alert Engine 開始掃描 {trade_date} ===")
    brokers  = await _get_watchlist_brokers()
    if not brokers:
        logger.info("Watchlist 為空，跳過警報掃描")
        return 0

    total_alerts = 0

    for bw in brokers:
        bcode = bw["broker_code"]
        bname = bw["broker_name"] or bcode
        bscore = bw.get("broker_score", 0) or 0
        history_dates = [str(d) for d in (bw.get("blood_selling_dates") or [])[:3]]

        actions = await _get_today_actions(trade_date, bcode)
        if not actions:
            continue

        for act in actions:
            stock_code = act["stock_code"]
            stock_name = act.get("stock_name", stock_code)
            net_vol    = int(act.get("net_volume", 0) or 0)
            abs_ratio  = float(act.get("absorption_ratio") or 0)
            net_amt    = float(act.get("net_amount") or 0)

            # ── 警報 1: 異常大買 ──────────────────────────────────────────────
            if abs_ratio >= ABS_RATIO_THRESHOLD or net_amt >= ABS_AMOUNT_THRESHOLD:
                if not await _already_alerted(trade_date, bcode, stock_code, "ABNORMAL_BUY"):
                    consec = await _get_consecutive_data(bcode, stock_code, trade_date, 5)
                    total_consec = sum(c["net_volume"] for c in consec)
                    msg = fmt_alert_abnormal_buy(
                        bcode, bname, stock_code, stock_name, trade_date,
                        net_vol, abs_ratio, net_amt, bscore,
                        bw.get("detected_sectors", ["—"])[0] if bw.get("detected_sectors") else "—",
                        history_dates,
                        consecutive_days=len(consec),
                        total_buy_consec=total_consec,
                    )
                    level = "CRITICAL" if abs_ratio >= 10 else "WARN"
                    await _save_alert(
                        trade_date, bcode, bname, stock_code, stock_name,
                        "ABNORMAL_BUY", level, net_vol, abs_ratio, net_amt,
                        len(consec), bscore, msg,
                    )
                    if send_alert(msg):
                        await _mark_sent(trade_date, bcode, stock_code, "ABNORMAL_BUY")
                    total_alerts += 1
                    logger.info(f"⚠️ ABNORMAL_BUY: {bname}/{stock_code} abs={abs_ratio:.1f}%")

            # ── 警報 2: 連續佈局 ──────────────────────────────────────────────
            consec = await _get_consecutive_data(bcode, stock_code, trade_date, 10)
            if len(consec) >= CONSECUTIVE_DAYS:
                if not await _already_alerted(trade_date, bcode, stock_code, "CONSECUTIVE"):
                    total_net  = sum(c["net_volume"] for c in consec)
                    total_amt  = sum(float(c.get("net_amount") or 0) for c in consec)
                    day_details = [
                        {"date": str(c["trade_date"]), "net_vol": c["net_volume"]}
                        for c in reversed(consec[:3])
                    ]
                    msg = fmt_alert_consecutive(
                        bcode, bname, stock_code, stock_name, trade_date,
                        len(consec), day_details, total_net, total_amt, None,
                        bscore, "連續佈局", history_dates,
                    )
                    await _save_alert(
                        trade_date, bcode, bname, stock_code, stock_name,
                        "CONSECUTIVE", "WARN", net_vol, abs_ratio, net_amt,
                        len(consec), bscore, msg,
                    )
                    if send_alert(msg):
                        await _mark_sent(trade_date, bcode, stock_code, "CONSECUTIVE")
                    total_alerts += 1
                    logger.info(f"🔥 CONSECUTIVE: {bname}/{stock_code} {len(consec)} days")

            # ── 警報 3: 隔日沖風險 ────────────────────────────────────────────
            if bw.get("day_trade_risk") == "HIGH" and net_vol > 500:
                if not await _already_alerted(trade_date, bcode, stock_code, "DAY_TRADE_RISK"):
                    dt = await calc_day_trade_score(bcode, trade_date)
                    msg = fmt_alert_day_trade_risk(
                        bcode, bname, stock_code, stock_name, trade_date,
                        dt["day_trade_score"], dt.get("day_trade_detail", ""),
                    )
                    await _save_alert(
                        trade_date, bcode, bname, stock_code, stock_name,
                        "DAY_TRADE_RISK", "WARN", net_vol, abs_ratio, net_amt,
                        1, bscore, msg,
                    )
                    if send_alert(msg):
                        await _mark_sent(trade_date, bcode, stock_code, "DAY_TRADE_RISK")
                    total_alerts += 1
                    logger.info(f"⚠️ DAY_TRADE_RISK: {bname}/{stock_code}")

    logger.info(f"=== Alert Engine 完成：{total_alerts} 則警報 ===")
    return total_alerts

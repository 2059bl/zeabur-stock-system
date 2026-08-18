"""
APScheduler — 每日自動執行流程
排程（台灣時間 UTC+8）：
  22:30  每日主流程（行情 → 法人 → 融資 → 分點 → 暴跌分析 → Alert）
  09:30  開盤前隔日沖風險提醒
  01:00  清理舊資料
"""
import asyncio
import logging
import os
from datetime import date, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.finmind_client import get_today_count, is_near_limit, verify_trading_dates
from src.universe import sync_universe, get_all_codes
from src.market_data import fetch_prices_batch, get_crash_stocks
from src.blood_absorption import analyze_blood_day
from src.broker_score import rebuild_watchlist_from_history, update_watchlist
from src.alert_engine import run_daily_alerts
from src.telegram import send_blood_summary, send_alert
from src.db import execute

logger = logging.getLogger(__name__)

CRASH_THRESHOLD = float(os.environ.get("CRASH_THRESHOLD", "-2.0"))


async def _get_taiex_drop(trade_date: str) -> float:
    """從 DB 取大盤跌幅（若 stock_daily 有 0050 可近似）。"""
    try:
        from src.db import fetch_one
        row = await fetch_one(
            "SELECT change_pct FROM stock_daily WHERE stock_code='0050' AND trade_date=$1",
            date.fromisoformat(trade_date),
        )
        return float(row["change_pct"]) if row else 0.0
    except Exception:
        return 0.0


async def _daily_pipeline():
    """每日 22:30 主流程。"""
    today = str(date.today())
    logger.info(f"===== 每日主流程開始 {today} =====")

    # ── P0: 確認交易日 ──────────────────────────────────────────────────────
    is_td = await verify_trading_dates([today])
    if not is_td.get(today, False):
        logger.info(f"{today} 非交易日，跳過")
        return

    # ── P1: API Quota 檢查 ──────────────────────────────────────────────────
    if is_near_limit():
        msg = f"⚠️ FinMind API 今日用量已達安全閾值（{get_today_count()}/8000），停止擴張"
        logger.warning(msg)
        send_alert(msg)
        return

    # ── P2: 同步 Universe ───────────────────────────────────────────────────
    await sync_universe()
    all_stocks = await get_all_codes()
    codes = [s["code"] for s in all_stocks]
    logger.info(f"追蹤標的：{len(codes)} 支")

    # ── P3: 取得行情（批量，DB-first）──────────────────────────────────────
    logger.info("取得行情資料...")
    price_map = await fetch_prices_batch(codes, today)
    fetched_ok = sum(1 for v in price_map.values() if v is not None)
    logger.info(f"行情完成：{fetched_ok}/{len(codes)}")

    # ── P4: 判斷是否為暴跌日 ────────────────────────────────────────────────
    taiex_drop = await _get_taiex_drop(today)
    is_crash = taiex_drop <= CRASH_THRESHOLD

    logger.info(f"大盤估算跌幅：{taiex_drop:.1f}%（閾值：{CRASH_THRESHOLD}%）")
    logger.info(f"暴跌日觸發：{'是' if is_crash else '否'}")

    results = []
    if is_crash:
        # ── P5: 暴跌日完整分析 ──────────────────────────────────────────────
        logger.info("執行帶血籌碼承接分析...")
        results = await analyze_blood_day(
            today,
            drop_threshold=-4.0,
            volume_ratio_min=1.5,
            max_stocks=60,
            taiex_drop=taiex_drop,
        )
        if results:
            send_blood_summary(today, results, taiex_drop)

            # 更新 watchlist
            for r in results:
                if r.get("is_blood_absorption"):
                    for b in r.get("top_brokers", [])[:3]:
                        await update_watchlist(
                            b.get("broker_code", ""),
                            b.get("broker_name", ""),
                            today,
                            [r["stock_code"]],
                            [r.get("sector", "")],
                            b.get("net_volume", 0),
                            b.get("absorption_ratio") or 0,
                            today,
                        )

    # ── P6: Alert Engine（Watchlist 掃描）──────────────────────────────────
    if not is_near_limit():
        n_alerts = await run_daily_alerts(today)
        logger.info(f"Alert Engine 完成：{n_alerts} 則警報")
    else:
        logger.warning("API 接近上限，跳過 Alert Engine")

    used = get_today_count()
    logger.info(f"===== 每日主流程完成 {today}，今日 API 用量：{used}/8000 =====")


async def _morning_risk_check():
    """每日 09:30 隔日沖風險提醒。"""
    today = str(date.today())
    logger.info(f"開盤前隔日沖風險掃描 {today}")

    from src.db import fetch_all
    high_risk = await fetch_all(
        """SELECT bw.broker_code, bw.broker_name,
                  array_agg(DISTINCT bda.stock_code) AS stocks
           FROM broker_watchlist bw
           JOIN broker_daily_actions bda ON bw.broker_code=bda.broker_code
           WHERE bw.day_trade_risk='HIGH'
             AND bda.trade_date=(SELECT MAX(trade_date) FROM broker_daily_actions)
             AND bda.net_volume > 0
           GROUP BY bw.broker_code, bw.broker_name"""
    )
    if high_risk:
        lines = ["⚠️【開盤前隔日沖風險提示】"]
        for r in high_risk:
            lines.append(f"  {r['broker_name']} ({r['broker_code']}): {', '.join(r['stocks'][:5])}")
        send_alert("\n".join(lines))


async def _cleanup():
    """每日 01:00 清理。"""
    try:
        await execute("SELECT cleanup_old_data_v5()")
        logger.info("資料清理完成")
    except Exception:
        await execute("SELECT cleanup_old_data()")


def start_scheduler() -> AsyncIOScheduler:
    tz = "Asia/Taipei"
    scheduler = AsyncIOScheduler(timezone=tz)

    scheduler.add_job(
        _daily_pipeline, CronTrigger(hour=22, minute=30, timezone=tz),
        id="daily_pipeline", name="每日主流程",
        replace_existing=True, max_instances=1,
    )
    scheduler.add_job(
        _morning_risk_check, CronTrigger(hour=9, minute=30, timezone=tz),
        id="morning_risk", name="開盤前風險",
        replace_existing=True, max_instances=1,
    )
    scheduler.add_job(
        _cleanup, CronTrigger(hour=1, minute=0, timezone=tz),
        id="cleanup", name="清理舊資料",
        replace_existing=True, max_instances=1,
    )

    scheduler.start()
    logger.info("排程器已啟動（22:30 主流程 / 09:30 風險提示 / 01:00 清理）")
    return scheduler

import os
import logging
from datetime import date, datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.data_agent import upsert_daily_prices, upsert_chip_data
from agents.analysis_agent import run_analysis, generate_text_report
from agents.bear_agent import run_dynamic_screening
from agents.decision_agent import run_cloud_decision
from utils.signals import compute_market_bear_signal
from utils.notifier import send_telegram, get_chat_id
from utils.db import execute, fetch_all, get_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

SCHEDULE_HOUR   = int(os.environ.get("SCHEDULE_HOUR", "22"))
SCHEDULE_MINUTE = int(os.environ.get("SCHEDULE_MINUTE", "0"))
SCHEDULE_TZ     = os.environ.get("SCHEDULE_TZ", "Asia/Taipei")

scheduler = AsyncIOScheduler(timezone=SCHEDULE_TZ)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    scheduler.add_job(
        _scheduled_daily_run,
        CronTrigger(hour=SCHEDULE_HOUR, minute=SCHEDULE_MINUTE, timezone=SCHEDULE_TZ),
        id="daily_pipeline",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"排程啟動：每日 {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} {SCHEDULE_TZ} 自動執行")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="台股 AI 量化系統", version="2.6.0", lifespan=lifespan)


# ─── Models ───────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    trade_date: Optional[str] = None


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "time": datetime.utcnow().isoformat(),
        "schedule": f"{SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} {SCHEDULE_TZ}",
    }


@app.post("/run/daily")
async def trigger_pipeline(req: RunRequest, background_tasks: BackgroundTasks):
    target_date = req.trade_date or str(date.today())
    background_tasks.add_task(_pipeline_worker, target_date)
    return {"status": "已排入背景執行", "date": target_date}


@app.get("/stocks")
async def list_stocks():
    return await fetch_all(
        "SELECT * FROM stocks WHERE is_active = TRUE ORDER BY market, stock_code"
    )


@app.get("/candidates")
async def bear_candidates():
    return await fetch_all("SELECT * FROM bear_strategy_candidates LIMIT 30")


@app.get("/reports/latest")
async def latest_reports():
    return await fetch_all("""
        SELECT ar.*, s.stock_name FROM ai_reports ar
        JOIN stocks s ON s.stock_code = ar.stock_code
        ORDER BY ar.created_at DESC LIMIT 20
    """)


@app.get("/setup/telegram")
async def setup_telegram():
    """查詢 Telegram chat_id（對 bot 發訊息後呼叫此端點）"""
    return await get_chat_id()


@app.get("/logs")
async def workflow_logs():
    return await fetch_all(
        "SELECT * FROM workflow_logs ORDER BY started_at DESC LIMIT 20"
    )


# ─── Pipeline ─────────────────────────────────────────────────────────────────

async def _scheduled_daily_run():
    today = str(date.today())
    logger.info(f"[排程] 自動觸發: {today}")
    await _pipeline_worker(today)


async def _pipeline_worker(trade_date: str):
    try:
        await execute(
            "INSERT INTO workflow_logs (workflow_name, status) VALUES ('DailyPipeline', 'RUNNING')"
        )

        stocks = await fetch_all(
            "SELECT stock_code, market FROM stocks WHERE is_active = TRUE"
        )

        # ── Step 1: 日K報價 ──────────────────────────────────────────────────
        price_ok = price_fail = 0
        for s in stocks:
            ok = await upsert_daily_prices(s["stock_code"], trade_date, s["market"])
            price_ok += ok
            price_fail += not ok
        logger.info(f"報價：成功 {price_ok}，失敗 {price_fail}")

        # ── Step 2: 技術指標 ─────────────────────────────────────────────────
        for s in stocks:
            await run_analysis(s["stock_code"], trade_date)

        # ── Step 3: FinMind 籌碼（三大法人 + 融資融券） ──────────────────────
        chip_ok = chip_fail = 0
        for s in stocks:
            ok = await upsert_chip_data(s["stock_code"], trade_date)
            chip_ok += ok
            chip_fail += not ok
        logger.info(f"籌碼：成功 {chip_ok}，失敗 {chip_fail}")

        # ── Step 4: 多空篩選 ─────────────────────────────────────────────────
        candidates = await run_dynamic_screening(trade_date)

        # ── Step 5: LLM 決策（僅空頭候選） ──────────────────────────────────
        decisions = []
        for c in candidates:
            ind_rows = await fetch_all(
                "SELECT * FROM latest_indicators WHERE stock_code = $1", c["stock_code"]
            )
            if not ind_rows:
                continue
            rep_text = generate_text_report(c["stock_code"], ind_rows[0])
            dec = await run_cloud_decision(c["stock_code"], trade_date, rep_text)
            decisions.append(
                f"{c['stock_code']} {c.get('stock_name','')} "
                f"→ *{dec.get('recommendation')}* (score:`{dec.get('final_score')}`)"
            )

        # ── Step 6: 大盤信號 ─────────────────────────────────────────────────
        mkt = await compute_market_bear_signal()

        # ── Step 7: Telegram 推播 ────────────────────────────────────────────
        candidate_block = (
            "\n".join([f"  • {d}" for d in decisions])
            if decisions else "  • 今日無符合條件標的"
        )
        msg = (
            f"📊 *台股 AI 量化日報* `{trade_date}`\n\n"
            f"*大盤風險*：`{mkt.level}`  分數 `{mkt.total_score}`\n"
            f"*法人動向*：`{mkt.institution_flow}`\n"
            f"*操作建議*：{mkt.action}\n\n"
            f"*空頭候選（{len(candidates)} 檔）*\n{candidate_block}\n\n"
            f"_報價 {price_ok}✓/{price_fail}✗  "
            f"籌碼 {chip_ok}✓/{chip_fail}✗_"
        )

        await execute("INSERT INTO notifications (message_text) VALUES ($1)", msg)
        sent = await send_telegram(msg)
        if sent:
            await execute(
                "UPDATE notifications SET is_sent=TRUE, sent_at_actual=NOW() WHERE is_sent=FALSE"
            )

        await execute(
            "UPDATE workflow_logs SET status='SUCCESS', finished_at=NOW() WHERE status='RUNNING'"
        )
        logger.info(f"[{trade_date}] 完成。Telegram: {'✓' if sent else '✗'}")

    except Exception as e:
        logger.exception(f"Pipeline 失敗: {e}")
        safe = str(e).replace("'", "''")[:500]
        await execute(
            f"UPDATE workflow_logs SET status='FAILED', finished_at=NOW(), "
            f"error_message='{safe}' WHERE status='RUNNING'"
        )

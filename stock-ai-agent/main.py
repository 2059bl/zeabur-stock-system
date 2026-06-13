import os
import asyncio
import logging
from datetime import date, datetime
from contextlib import asynccontextmanager
from typing import Optional, List

from fastapi import FastAPI, BackgroundTasks, Query
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.data_agent import upsert_daily_prices, upsert_chip_data, backfill_prices
from agents.analysis_agent import run_analysis, generate_text_report
from agents.bear_agent import run_dynamic_screening
from agents.decision_agent import run_cloud_decision
from utils.signals import compute_market_bear_signal
from utils.notifier import send_telegram, get_chat_id
from utils.chatbot import send_message, chat_with_llm, set_webhook
from utils.db import execute, fetch_all, get_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

SCHEDULE_HOUR   = int(os.environ.get("SCHEDULE_HOUR", "22"))
SCHEDULE_MINUTE = int(os.environ.get("SCHEDULE_MINUTE", "0"))
SCHEDULE_TZ     = os.environ.get("SCHEDULE_TZ", "Asia/Taipei")
CONCURRENCY     = int(os.environ.get("PIPELINE_CONCURRENCY", "8"))

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


app = FastAPI(title="台股 AI 量化系統", version="3.0.0", lifespan=lifespan)


# ─── Models ───────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    trade_date: Optional[str] = None


class StockAdd(BaseModel):
    stock_code: str
    stock_name: str
    market: str = "TWSE"
    sector: Optional[str] = None
    industry: Optional[str] = None


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    count = await fetch_all("SELECT COUNT(*) AS n FROM stocks WHERE is_active=TRUE")
    return {
        "status": "ok",
        "time": datetime.utcnow().isoformat(),
        "schedule": f"{SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} {SCHEDULE_TZ}",
        "tracking_stocks": count[0]["n"] if count else 0,
    }


@app.post("/run/daily")
async def trigger_pipeline(req: RunRequest, background_tasks: BackgroundTasks):
    target_date = req.trade_date or str(date.today())
    background_tasks.add_task(_pipeline_worker, target_date)
    return {"status": "已排入背景執行", "date": target_date}


@app.get("/stocks")
async def list_stocks():
    return await fetch_all(
        "SELECT * FROM stocks WHERE is_active = TRUE ORDER BY market, sector, stock_code"
    )


@app.post("/stocks")
async def add_stock(s: StockAdd):
    """新增追蹤股票"""
    await execute("""
        INSERT INTO stocks (stock_code, stock_name, market, sector, industry)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (stock_code) DO UPDATE SET
            stock_name = EXCLUDED.stock_name,
            is_active  = TRUE,
            sector     = COALESCE(EXCLUDED.sector, stocks.sector),
            industry   = COALESCE(EXCLUDED.industry, stocks.industry)
    """, s.stock_code, s.stock_name, s.market, s.sector, s.industry)
    return {"status": "ok", "stock_code": s.stock_code}


@app.delete("/stocks/{stock_code}")
async def remove_stock(stock_code: str):
    """停止追蹤（軟刪除，保留歷史資料）"""
    await execute(
        "UPDATE stocks SET is_active = FALSE WHERE stock_code = $1", stock_code
    )
    return {"status": "ok", "stock_code": stock_code}


@app.get("/screen")
async def screen_stocks(
    bear_alignment: Optional[bool] = Query(None, description="空頭排列 5MA<20MA<60MA"),
    rsi_max: Optional[float] = Query(None, description="RSI 上限（如 45）"),
    rsi_min: Optional[float] = Query(None, description="RSI 下限（如 20）"),
    institution_flow: Optional[str] = Query(None, description="DOUBLE_SELL / SINGLE_SELL / HOLD_OR_BUY"),
    sector: Optional[str] = Query(None, description="產業篩選，如 半導體"),
    market: Optional[str] = Query(None, description="TWSE 或 OTC"),
    macd_bearish: Optional[bool] = Query(None, description="MACD 柱狀體 < 0"),
    limit: int = Query(30, le=100),
):
    """
    量化選股端點。不填參數 = 撈全部最新指標。
    可組合任意條件：
      GET /screen?bear_alignment=true&rsi_max=45&institution_flow=DOUBLE_SELL
      GET /screen?sector=半導體&macd_bearish=true
    """
    conditions = ["1=1"]
    params: list = []
    idx = 1

    if bear_alignment is not None:
        conditions.append(f"li.is_bear_alignment = ${idx}")
        params.append(bear_alignment); idx += 1

    if rsi_max is not None:
        conditions.append(f"li.rsi_14 <= ${idx}")
        params.append(rsi_max); idx += 1

    if rsi_min is not None:
        conditions.append(f"li.rsi_14 >= ${idx}")
        params.append(rsi_min); idx += 1

    if institution_flow:
        conditions.append(f"li.institution_flow = ${idx}::institution_flow_type")
        params.append(institution_flow); idx += 1

    if macd_bearish is not None:
        op = "<" if macd_bearish else ">="
        conditions.append(f"li.macd_histogram {op} 0")

    if sector:
        conditions.append(f"li.sector = ${idx}")
        params.append(sector); idx += 1

    if market:
        conditions.append(f"s.market = ${idx}")
        params.append(market); idx += 1

    params.append(limit); idx += 1

    sql = f"""
        SELECT
            li.stock_code, s.stock_name, s.market, s.sector,
            li.trade_date,
            li.sma_5, li.sma_20, li.sma_60,
            li.rsi_14, li.macd_histogram, li.bias_rate,
            li.is_bear_alignment, li.short_trend_confirmed,
            li.institution_flow,
            li.foreign_net_buy, li.investment_trust_net_buy,
            li.margin_balance, li.short_to_margin_ratio,
            ar.recommendation AS ai_rec, ar.final_score AS ai_score
        FROM latest_indicators li
        JOIN stocks s ON s.stock_code = li.stock_code
        LEFT JOIN LATERAL (
            SELECT recommendation, final_score FROM ai_reports
            WHERE stock_code = li.stock_code AND report_type = 'DECISION'
            ORDER BY created_at DESC LIMIT 1
        ) ar ON TRUE
        WHERE {" AND ".join(conditions)}
        ORDER BY li.rsi_14 ASC NULLS LAST
        LIMIT ${idx - 1}
    """
    return await fetch_all(sql, *params)


@app.get("/candidates")
async def bear_candidates():
    return await fetch_all("SELECT * FROM bear_strategy_candidates LIMIT 30")


@app.get("/reports/latest")
async def latest_reports():
    return await fetch_all("""
        SELECT ar.*, s.stock_name, s.sector FROM ai_reports ar
        JOIN stocks s ON s.stock_code = ar.stock_code
        ORDER BY ar.created_at DESC LIMIT 20
    """)


@app.get("/setup/telegram")
async def setup_telegram():
    return await get_chat_id()


@app.post("/setup/webhook")
async def setup_webhook():
    """向 Telegram 登記 Webhook，讓 bot 能接收訊息並回覆。"""
    base = os.environ.get("SERVICE_URL", "https://twstock-agent-1781283629.zeabur.app")
    result = await set_webhook(f"{base}/telegram/webhook")
    return result


@app.post("/telegram/webhook")
async def telegram_webhook(update: dict, background_tasks: BackgroundTasks):
    """Telegram 推送入口：接收訊息，在背景生成回覆。"""
    background_tasks.add_task(_handle_telegram_update, update)
    return {"ok": True}


async def _handle_telegram_update(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = str(msg["chat"]["id"])
    text    = msg.get("text", "").strip()
    if not text:
        return

    # ── 指令處理 ──────────────────────────────────────────────────────────
    if text.startswith("/start") or text.startswith("/help"):
        await send_message(chat_id, (
            "👋 *台股 AI 量化助理*\n\n"
            "你可以直接用中文問我問題，例如：\n"
            "  「現在有哪些空頭股票？」\n"
            "  「RSI 是什麼意思？」\n"
            "  「台積電最新指標怎樣？」\n\n"
            "📌 *快捷指令*\n"
            "/stocks — 查看追蹤的 50 檔股票\n"
            "/screen — 今日空頭候選\n"
            "/run — 立即觸發今日分析\n"
            "/help — 顯示此說明"
        ))
        return

    if text.startswith("/stocks"):
        rows = await fetch_all(
            "SELECT stock_code, stock_name, market, sector FROM stocks WHERE is_active=TRUE ORDER BY sector, stock_code"
        )
        by_sector: dict = {}
        for r in rows:
            by_sector.setdefault(r["sector"] or "其他", []).append(f"{r['stock_code']} {r['stock_name']}")
        lines = [f"📋 *追蹤股票（共 {len(rows)} 檔）*\n"]
        for sector, codes in sorted(by_sector.items()):
            lines.append(f"*{sector}*：{', '.join(codes)}")
        await send_message(chat_id, "\n".join(lines))
        return

    if text.startswith("/screen"):
        rows = await fetch_all("""
            SELECT li.stock_code, s.stock_name, li.rsi_14, li.is_bear_alignment, li.institution_flow
            FROM latest_indicators li JOIN stocks s ON s.stock_code=li.stock_code
            WHERE li.is_bear_alignment=TRUE
            ORDER BY li.rsi_14 ASC NULLS LAST LIMIT 15
        """)
        if not rows:
            await send_message(chat_id, "今日無空頭排列標的。")
        else:
            lines = [f"🔻 *空頭候選（{len(rows)} 檔）*\n"]
            for r in rows:
                rsi = f"{r['rsi_14']:.1f}" if r.get("rsi_14") else "N/A"
                lines.append(f"`{r['stock_code']}` {r['stock_name']}  RSI:{rsi}  法人:{r['institution_flow']}")
            await send_message(chat_id, "\n".join(lines))
        return

    if text.startswith("/run"):
        today = str(date.today())
        await send_message(chat_id, f"⚙️ 已觸發 {today} 分析，約 1-2 分鐘後推播結果。")
        await _pipeline_worker(today)
        return

    # ── 自由對話（LLM）─────────────────────────────────────────────────────
    # 附上最新指標摘要作為 context
    context_rows = await fetch_all("""
        SELECT li.stock_code, s.stock_name, li.rsi_14, li.is_bear_alignment,
               li.institution_flow, li.sma_5, li.sma_20
        FROM latest_indicators li JOIN stocks s ON s.stock_code=li.stock_code
        WHERE li.rsi_14 IS NOT NULL
        ORDER BY li.rsi_14 ASC LIMIT 10
    """)
    if context_rows:
        context = "目前 RSI 最低的 10 檔股票：\n" + "\n".join(
            f"{r['stock_code']} {r['stock_name']}: RSI={r['rsi_14']:.1f}, "
            f"空頭排列={r['is_bear_alignment']}, 法人={r['institution_flow']}"
            for r in context_rows if r.get("rsi_14")
        )
    else:
        context = None

    reply = await chat_with_llm(text, context)
    await send_message(chat_id, reply)


@app.get("/logs")
async def workflow_logs():
    return await fetch_all(
        "SELECT * FROM workflow_logs ORDER BY started_at DESC LIMIT 20"
    )


@app.post("/backfill")
async def backfill(
    days: int = Query(90, le=365, description="回填天數"),
    background_tasks: BackgroundTasks = None,
):
    """
    用 Yahoo Finance 補抓所有追蹤股票的歷史 K 線，讓技術指標有足夠資料計算。
    days=90 約需 1-2 分鐘，在背景執行。
    """
    background_tasks.add_task(_backfill_worker, days)
    return {"status": "回填已排入背景", "days": days}


async def _backfill_worker(days: int):
    stocks = await fetch_all("SELECT stock_code FROM stocks WHERE is_active = TRUE")
    sem = asyncio.Semaphore(6)
    results = await asyncio.gather(*[
        _run_with_semaphore(sem, backfill_prices(s["stock_code"], days))
        for s in stocks
    ])
    total = sum(results)
    logger.info(f"[Backfill] 完成，共寫入 {total} 筆歷史報價")

    # 回填完後重跑最新一天的指標
    latest_date = str(date.today())
    for s in stocks:
        await run_analysis(s["stock_code"], latest_date)
    logger.info("[Backfill] 技術指標重算完畢")


# ─── Pipeline ─────────────────────────────────────────────────────────────────

async def _scheduled_daily_run():
    today = str(date.today())
    logger.info(f"[排程] 自動觸發: {today}")
    await _pipeline_worker(today)


async def _run_with_semaphore(sem: asyncio.Semaphore, coro):
    async with sem:
        return await coro


async def _pipeline_worker(trade_date: str):
    try:
        await execute(
            "INSERT INTO workflow_logs (workflow_name, status) VALUES ('DailyPipeline', 'RUNNING')"
        )

        stocks = await fetch_all(
            "SELECT stock_code, market FROM stocks WHERE is_active = TRUE"
        )
        sem = asyncio.Semaphore(CONCURRENCY)

        # ── Step 1: 日K報價（並行） ──────────────────────────────────────────
        price_results = await asyncio.gather(*[
            _run_with_semaphore(sem, upsert_daily_prices(s["stock_code"], trade_date, s["market"]))
            for s in stocks
        ])
        price_ok = sum(price_results)
        price_fail = len(stocks) - price_ok
        logger.info(f"報價：成功 {price_ok}，失敗 {price_fail}")

        # ── Step 2: 技術指標（並行） ─────────────────────────────────────────
        await asyncio.gather(*[
            _run_with_semaphore(sem, run_analysis(s["stock_code"], trade_date))
            for s in stocks
        ])

        # ── Step 3: FinMind 籌碼（並行，限速避免 rate limit） ────────────────
        chip_sem = asyncio.Semaphore(4)  # FinMind 限速較嚴，並行數降低
        chip_results = await asyncio.gather(*[
            _run_with_semaphore(chip_sem, upsert_chip_data(s["stock_code"], trade_date))
            for s in stocks
        ])
        chip_ok = sum(chip_results)
        chip_fail = len(stocks) - chip_ok
        logger.info(f"籌碼：成功 {chip_ok}，失敗 {chip_fail}")

        # ── Step 4: 多空篩選 ─────────────────────────────────────────────────
        candidates = await run_dynamic_screening(trade_date)

        # ── Step 5: LLM 決策（僅空頭候選，並行） ────────────────────────────
        llm_sem = asyncio.Semaphore(3)

        async def _decide_one(c):
            ind_rows = await fetch_all(
                "SELECT * FROM latest_indicators WHERE stock_code = $1", c["stock_code"]
            )
            if not ind_rows:
                return None
            rep_text = generate_text_report(c["stock_code"], ind_rows[0])
            dec = await run_cloud_decision(c["stock_code"], trade_date, rep_text)
            return (
                f"{c['stock_code']} {c.get('stock_name','')} "
                f"→ *{dec.get('recommendation')}* (score:`{dec.get('final_score')}`)"
            )

        decision_results = await asyncio.gather(*[
            _run_with_semaphore(llm_sem, _decide_one(c)) for c in candidates
        ])
        decisions = [d for d in decision_results if d]

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
            f"_追蹤 {len(stocks)} 檔  "
            f"報價 {price_ok}✓/{price_fail}✗  "
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

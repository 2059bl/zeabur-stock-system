import os
import asyncio
import logging
from datetime import date, datetime
from contextlib import asynccontextmanager
from typing import Optional, List

from fastapi import FastAPI, BackgroundTasks, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.data_agent import upsert_daily_prices, upsert_chip_data, backfill_prices
from agents.analysis_agent import run_analysis, generate_text_report
from agents.bear_agent import run_dynamic_screening
from agents.decision_agent import run_cloud_decision
from agents.news_agent import run_news_sentiment, get_recent_news
from agents.scoring_agent import update_composite_scores
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


app = FastAPI(title="台股 AI 量化系統", version="4.0.0", lifespan=lifespan)


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
        "version": "4.0.0",
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
    await execute(
        "UPDATE stocks SET is_active = FALSE WHERE stock_code = $1", stock_code
    )
    return {"status": "ok", "stock_code": stock_code}


@app.get("/screen")
async def screen_stocks(
    bear_alignment: Optional[bool] = Query(None),
    rsi_max: Optional[float] = Query(None),
    rsi_min: Optional[float] = Query(None),
    institution_flow: Optional[str] = Query(None),
    sector: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    macd_bearish: Optional[bool] = Query(None),
    min_score: Optional[float] = Query(None, description="最低綜合評分"),
    limit: int = Query(30, le=100),
):
    """
    量化選股端點。支援多維度篩選 + 綜合評分排序。
    GET /screen?bear_alignment=true&rsi_max=45&min_score=50
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

    if min_score is not None:
        conditions.append(f"li.composite_score >= ${idx}")
        params.append(min_score); idx += 1

    params.append(limit)

    sql = f"""
        SELECT
            li.stock_code, s.stock_name, s.market, s.sector,
            li.trade_date,
            li.sma_5, li.sma_20, li.sma_60,
            li.rsi_14, li.macd_histogram, li.bias_rate,
            li.is_bear_alignment, li.short_trend_confirmed,
            li.institution_flow,
            li.foreign_net_buy, li.investment_trust_net_buy,
            li.foreign_consecutive_days, li.foreign_holding_ratio,
            li.margin_balance, li.short_to_margin_ratio,
            li.short_cover_days, li.margin_trend_5d,
            li.composite_score, li.sentiment_score,
            ar.recommendation AS ai_rec, ar.final_score AS ai_score
        FROM latest_indicators li
        JOIN stocks s ON s.stock_code = li.stock_code
        LEFT JOIN LATERAL (
            SELECT recommendation, final_score FROM ai_reports
            WHERE stock_code = li.stock_code AND report_type = 'DECISION'
            ORDER BY created_at DESC LIMIT 1
        ) ar ON TRUE
        WHERE {" AND ".join(conditions)}
        ORDER BY li.composite_score DESC NULLS LAST, li.rsi_14 ASC NULLS LAST
        LIMIT ${idx}
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


@app.get("/news/{stock_code}")
async def stock_news(stock_code: str, days: int = Query(7, le=30)):
    """查詢個股最近新聞與情緒分數。"""
    return await get_recent_news(stock_code, days)


@app.post("/backtest")
async def run_backtest(
    strategy: str = Query("bear_alignment", description="bear_alignment / double_sell / composite_60"),
    start_date: str = Query(..., description="YYYY-MM-DD"),
    end_date: str = Query(..., description="YYYY-MM-DD"),
    background_tasks: BackgroundTasks = None,
):
    """
    回測歷史選股訊號勝率。
    在背景執行，完成後結果寫入 backtest_results。
    GET /backtest/results 查看歷史回測結果。
    """
    background_tasks.add_task(_backtest_worker, strategy, start_date, end_date)
    return {"status": "回測已排入背景", "strategy": strategy, "period": f"{start_date}~{end_date}"}


@app.get("/backtest/results")
async def backtest_results():
    return await fetch_all(
        "SELECT * FROM backtest_results ORDER BY created_at DESC LIMIT 20"
    )


@app.get("/setup/telegram")
async def setup_telegram():
    return await get_chat_id()


@app.post("/setup/webhook")
async def setup_webhook():
    base = os.environ.get("SERVICE_URL", "https://twstock-agent-1781283629.zeabur.app")
    result = await set_webhook(f"{base}/telegram/webhook")
    return result


_MIGRATION_V3 = """
ALTER TABLE stock_indicators
    ADD COLUMN IF NOT EXISTS foreign_holding_ratio    NUMERIC(6,2),
    ADD COLUMN IF NOT EXISTS foreign_consecutive_days INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS short_cover_days         NUMERIC(8,2),
    ADD COLUMN IF NOT EXISTS margin_trend_5d          NUMERIC(6,2),
    ADD COLUMN IF NOT EXISTS composite_score          NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS sentiment_score          NUMERIC(5,2);
CREATE TABLE IF NOT EXISTS news_cache (
    id          BIGSERIAL PRIMARY KEY,
    stock_code  VARCHAR(10) REFERENCES stocks(stock_code),
    news_date   DATE NOT NULL,
    title       TEXT NOT NULL,
    source      VARCHAR(100),
    sentiment   NUMERIC(4,3),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(stock_code, news_date, title)
);
CREATE INDEX IF NOT EXISTS idx_news_code_date ON news_cache(stock_code, news_date DESC);
CREATE TABLE IF NOT EXISTS backtest_results (
    id              BIGSERIAL PRIMARY KEY,
    strategy_name   VARCHAR(100) NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    total_signals   INT,
    win_signals     INT,
    win_rate        NUMERIC(5,2),
    avg_return_5d   NUMERIC(6,2),
    avg_return_10d  NUMERIC(6,2),
    avg_return_20d  NUMERIC(6,2),
    max_drawdown    NUMERIC(6,2),
    sharpe_ratio    NUMERIC(6,3),
    details         JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE OR REPLACE VIEW latest_indicators AS
SELECT DISTINCT ON (i.stock_code)
    i.*, s.stock_name, s.sector, s.industry,
    CASE WHEN i.sma_5 < i.sma_20 AND i.sma_20 < i.sma_60 THEN TRUE ELSE FALSE END AS is_bear_alignment,
    CASE
        WHEN i.foreign_net_buy < 0 AND i.investment_trust_net_buy < 0 THEN 'DOUBLE_SELL'::institution_flow_type
        WHEN i.foreign_net_buy < 0 OR  i.investment_trust_net_buy < 0 THEN 'SINGLE_SELL'::institution_flow_type
        ELSE 'HOLD_OR_BUY'::institution_flow_type
    END AS institution_flow_signal
FROM stock_indicators i
JOIN stocks s ON s.stock_code = i.stock_code
ORDER BY i.stock_code, i.trade_date DESC;
CREATE OR REPLACE VIEW bear_strategy_candidates AS
SELECT
    li.*, ar.final_score AS ai_score, ar.recommendation AS ai_recommendation,
    ar.cot_reasoning AS ai_reasoning, mi.market_signal_score, mi.market_signal_level
FROM latest_indicators li
LEFT JOIN LATERAL (
    SELECT * FROM ai_reports
    WHERE stock_code = li.stock_code AND report_type = 'DECISION'
    ORDER BY created_at DESC LIMIT 1
) ar ON TRUE
LEFT JOIN market_indicators mi ON mi.trade_date = li.trade_date
WHERE li.is_bear_alignment = TRUE
  AND li.institution_flow_signal IN ('DOUBLE_SELL','SINGLE_SELL')
ORDER BY li.composite_score DESC NULLS LAST, li.bear_signal_score DESC NULLS LAST
"""


@app.post("/setup/migrate")
async def run_migration():
    """執行 migration v3：新增外資持股、評分、新聞、回測等欄位與表格。"""
    statements = [s.strip() for s in _MIGRATION_V3.split(";") if s.strip() and not s.strip().startswith("--")]
    errors = []
    for stmt in statements:
        try:
            await execute(stmt)
        except Exception as e:
            errors.append(str(e)[:200])
    return {"status": "done", "statements": len(statements), "errors": errors}


@app.post("/telegram/webhook")
async def telegram_webhook(update: dict, background_tasks: BackgroundTasks):
    background_tasks.add_task(_handle_telegram_update, update)
    return {"ok": True}


# ─── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """即時監控儀表板（純 HTML，無需前端框架）。"""
    rows = await fetch_all("""
        SELECT
            li.stock_code, s.stock_name, s.sector, s.market,
            li.trade_date,
            li.rsi_14, li.macd_histogram, li.bias_rate,
            li.is_bear_alignment, li.institution_flow,
            li.foreign_consecutive_days, li.foreign_holding_ratio,
            li.short_to_margin_ratio, li.short_cover_days,
            li.composite_score, li.sentiment_score,
            ar.recommendation AS ai_rec
        FROM latest_indicators li
        JOIN stocks s ON s.stock_code = li.stock_code
        LEFT JOIN LATERAL (
            SELECT recommendation FROM ai_reports
            WHERE stock_code = li.stock_code AND report_type = 'DECISION'
            ORDER BY created_at DESC LIMIT 1
        ) ar ON TRUE
        ORDER BY li.composite_score DESC NULLS LAST
    """)

    mkt = await compute_market_bear_signal()

    def fmt(v, decimals=2):
        if v is None:
            return "<span style='color:#888'>—</span>"
        return f"{float(v):.{decimals}f}"

    def score_color(score):
        if score is None:
            return "#888"
        s = float(score)
        if s >= 70:
            return "#e74c3c"
        if s >= 45:
            return "#e67e22"
        if s >= 20:
            return "#f1c40f"
        return "#27ae60"

    def bear_badge(is_bear):
        if is_bear:
            return "<span style='background:#e74c3c;color:#fff;padding:2px 6px;border-radius:3px;font-size:11px'>空頭</span>"
        return "<span style='background:#27ae60;color:#fff;padding:2px 6px;border-radius:3px;font-size:11px'>正常</span>"

    def flow_badge(flow):
        colors = {"DOUBLE_SELL": "#e74c3c", "SINGLE_SELL": "#e67e22", "HOLD_OR_BUY": "#27ae60", "DOUBLE_BUY": "#2980b9"}
        c = colors.get(str(flow), "#888")
        labels = {"DOUBLE_SELL": "雙賣超", "SINGLE_SELL": "單賣超", "HOLD_OR_BUY": "持有/買", "DOUBLE_BUY": "雙買超"}
        return f"<span style='background:{c};color:#fff;padding:2px 6px;border-radius:3px;font-size:11px'>{labels.get(str(flow),str(flow))}</span>"

    rows_html = ""
    for r in rows:
        sc = r.get("composite_score")
        sc_color = score_color(sc)
        sc_str = f"{float(sc):.0f}" if sc is not None else "—"
        consec = r.get("foreign_consecutive_days") or 0
        consec_str = (f"<span style='color:#e74c3c'>連賣{abs(consec)}日</span>" if consec < 0
                      else f"<span style='color:#27ae60'>連買{consec}日</span>" if consec > 0
                      else "—")
        ai_rec = r.get("ai_rec") or "—"
        rows_html += f"""
        <tr>
          <td><b>{r['stock_code']}</b></td>
          <td>{r['stock_name']}</td>
          <td><small>{r.get('sector') or ''}</small></td>
          <td style='text-align:center'>{bear_badge(r.get('is_bear_alignment'))}</td>
          <td style='text-align:right'>{fmt(r.get('rsi_14'))}</td>
          <td style='text-align:right'>{fmt(r.get('macd_histogram'),4)}</td>
          <td style='text-align:center'>{flow_badge(r.get('institution_flow'))}</td>
          <td style='text-align:center'>{consec_str}</td>
          <td style='text-align:right'>{fmt(r.get('foreign_holding_ratio'))}%</td>
          <td style='text-align:right'>{fmt(r.get('short_to_margin_ratio'))}%</td>
          <td style='text-align:right'>{fmt(r.get('short_cover_days'),1)}日</td>
          <td style='text-align:center;font-weight:bold;color:{sc_color}'>{sc_str}</td>
          <td style='text-align:center'><small>{ai_rec}</small></td>
        </tr>"""

    mkt_color = {"NORMAL": "#27ae60", "WATCH": "#2980b9", "WARNING": "#e67e22", "DANGER": "#e74c3c", "EXTREME": "#8e44ad"}.get(mkt.level, "#888")
    bear_count = sum(1 for r in rows if r.get("is_bear_alignment"))
    high_score = sum(1 for r in rows if r.get("composite_score") and float(r["composite_score"]) >= 60)

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>台股 AI 量化系統｜監控儀表板</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; padding: 20px; }}
  h1 {{ font-size: 20px; margin-bottom: 16px; color: #fff; }}
  .cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }}
  .card {{ background: #1a1d27; border-radius: 8px; padding: 14px 20px; min-width: 160px; }}
  .card .label {{ font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: .5px; }}
  .card .value {{ font-size: 28px; font-weight: bold; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; background: #1a1d27; border-radius: 8px; overflow: hidden; font-size: 13px; }}
  th {{ background: #252836; padding: 10px 8px; text-align: left; font-size: 11px; color: #aaa; text-transform: uppercase; white-space: nowrap; }}
  td {{ padding: 8px 8px; border-bottom: 1px solid #252836; }}
  tr:hover td {{ background: #252836; }}
  .refresh {{ float: right; font-size: 12px; color: #888; margin-top: -40px; }}
</style>
<meta http-equiv="refresh" content="300">
</head>
<body>
<h1>📊 台股 AI 量化系統 v4.0</h1>
<div class="refresh">每 5 分鐘自動刷新 · {datetime.now().strftime('%H:%M:%S')}</div>
<div class="cards">
  <div class="card">
    <div class="label">追蹤股票</div>
    <div class="value" style="color:#3498db">{len(rows)}</div>
  </div>
  <div class="card">
    <div class="label">空頭排列</div>
    <div class="value" style="color:#e74c3c">{bear_count}</div>
  </div>
  <div class="card">
    <div class="label">高評分（≥60）</div>
    <div class="value" style="color:#e67e22">{high_score}</div>
  </div>
  <div class="card">
    <div class="label">大盤風險</div>
    <div class="value" style="color:{mkt_color}">{mkt.level}</div>
  </div>
  <div class="card">
    <div class="label">大盤分數</div>
    <div class="value" style="color:{mkt_color}">{mkt.total_score}</div>
  </div>
  <div class="card">
    <div class="label">期貨外資淨部位</div>
    <div class="value" style="color:{'#e74c3c' if mkt.futures_foreign_net < 0 else '#27ae60'}">{mkt.futures_foreign_net:+,}</div>
  </div>
</div>
<table>
<thead>
  <tr>
    <th>代碼</th><th>名稱</th><th>產業</th><th>均線</th><th>RSI</th>
    <th>MACD柱</th><th>法人流向</th><th>外資連續</th><th>外資持股%</th>
    <th>券資比%</th><th>回補天數</th><th>綜合評分</th><th>AI建議</th>
  </tr>
</thead>
<tbody>{rows_html}</tbody>
</table>
</body>
</html>"""
    return HTMLResponse(content=html)


# ─── Telegram Handler ─────────────────────────────────────────────────────────

async def _handle_telegram_update(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = str(msg["chat"]["id"])
    text    = msg.get("text", "").strip()
    if not text:
        return

    if text.startswith("/start") or text.startswith("/help"):
        await send_message(chat_id, (
            "👋 *台股 AI 量化助理 v4.0*\n\n"
            "直接用中文問我，例如：\n"
            "「現在有哪些空頭股票？」\n"
            "「2330 外資最近在做什麼？」\n"
            "「RSI 和空頭排列怎麼看？」\n\n"
            "📌 *快捷指令*\n"
            "/stocks — 追蹤的 50 檔股票\n"
            "/screen — 今日空頭候選（含評分）\n"
            "/top — 綜合評分 TOP 10\n"
            "/market — 大盤氛圍與期貨外資\n"
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
            SELECT li.stock_code, s.stock_name, li.rsi_14, li.is_bear_alignment,
                   li.institution_flow, li.composite_score, li.foreign_consecutive_days
            FROM latest_indicators li JOIN stocks s ON s.stock_code=li.stock_code
            WHERE li.is_bear_alignment=TRUE
            ORDER BY li.composite_score DESC NULLS LAST, li.rsi_14 ASC NULLS LAST LIMIT 15
        """)
        if not rows:
            await send_message(chat_id, "今日無空頭排列標的。")
        else:
            lines = [f"🔻 *空頭候選（{len(rows)} 檔）*\n"]
            for r in rows:
                rsi  = f"{r['rsi_14']:.1f}" if r.get("rsi_14") else "N/A"
                sc   = f"{r['composite_score']:.0f}" if r.get("composite_score") else "—"
                consec = r.get("foreign_consecutive_days") or 0
                c_str = f"連賣{abs(consec)}日" if consec < 0 else ""
                lines.append(f"`{r['stock_code']}` {r['stock_name']}  RSI:{rsi}  評分:{sc}  {c_str}")
            await send_message(chat_id, "\n".join(lines))
        return

    if text.startswith("/top"):
        rows = await fetch_all("""
            SELECT li.stock_code, s.stock_name, li.composite_score,
                   li.rsi_14, li.is_bear_alignment, li.institution_flow
            FROM latest_indicators li JOIN stocks s ON s.stock_code=li.stock_code
            WHERE li.composite_score IS NOT NULL
            ORDER BY li.composite_score DESC LIMIT 10
        """)
        lines = ["🏆 *綜合評分 TOP 10*\n"]
        for i, r in enumerate(rows, 1):
            sc   = f"{r['composite_score']:.0f}"
            rsi  = f"{r['rsi_14']:.1f}" if r.get("rsi_14") else "N/A"
            bear = "🔻" if r.get("is_bear_alignment") else "  "
            lines.append(f"{i}. {bear}`{r['stock_code']}` {r['stock_name']}  評分:{sc}  RSI:{rsi}")
        await send_message(chat_id, "\n".join(lines))
        return

    if text.startswith("/market"):
        mkt = await compute_market_bear_signal(str(date.today()))
        level_emoji = {"NORMAL": "🟢", "WATCH": "🔵", "WARNING": "🟡", "DANGER": "🟠", "EXTREME": "🔴"}.get(mkt.level, "⚪")
        await send_message(chat_id, (
            f"🌐 *大盤氛圍報告*\n\n"
            f"{level_emoji} *風險等級*：`{mkt.level}`  分數：`{mkt.total_score}`\n"
            f"*法人動向*：`{mkt.institution_flow}`\n"
            f"*外資連續賣超*：{mkt.foreign_sell_days} 日\n"
            f"*期貨外資淨部位*：`{mkt.futures_foreign_net:+,}` 口\n"
            f"*操作建議*：{mkt.action}"
        ))
        return

    if text.startswith("/run"):
        today = str(date.today())
        await send_message(chat_id, f"⚙️ 已觸發 {today} 分析，約 2-3 分鐘後推播結果。")
        await _pipeline_worker(today)
        return

    # ── 自由中文對話（附近期指標為 context）─────────────────────────────────
    context_rows = await fetch_all("""
        SELECT li.stock_code, s.stock_name, li.rsi_14, li.is_bear_alignment,
               li.institution_flow, li.composite_score,
               li.foreign_consecutive_days, li.foreign_holding_ratio,
               li.short_to_margin_ratio
        FROM latest_indicators li JOIN stocks s ON s.stock_code=li.stock_code
        WHERE li.composite_score IS NOT NULL
        ORDER BY li.composite_score DESC LIMIT 10
    """)
    if context_rows:
        context = "目前綜合評分最高的 10 檔股票：\n" + "\n".join(
            f"{r['stock_code']} {r['stock_name']}: 評分={r.get('composite_score') or 0:.0f}, "
            f"RSI={r['rsi_14']:.1f if r.get('rsi_14') else 'N/A'}, "
            f"空頭排列={r['is_bear_alignment']}, 法人={r['institution_flow']}, "
            f"外資連續={r.get('foreign_consecutive_days') or 0}日, "
            f"外資持股={r.get('foreign_holding_ratio') or 0:.1f}%"
            for r in context_rows if r.get("rsi_14")
        )
    else:
        context = None

    reply = await chat_with_llm(text, context)
    await send_message(chat_id, reply)


# ─── Logs ─────────────────────────────────────────────────────────────────────

@app.get("/logs")
async def workflow_logs():
    return await fetch_all(
        "SELECT * FROM workflow_logs ORDER BY started_at DESC LIMIT 20"
    )


@app.post("/backfill")
async def backfill(
    days: int = Query(90, le=365),
    background_tasks: BackgroundTasks = None,
):
    background_tasks.add_task(_backfill_worker, days)
    return {"status": "回填已排入背景", "days": days}


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
        sem      = asyncio.Semaphore(CONCURRENCY)
        chip_sem = asyncio.Semaphore(4)
        llm_sem  = asyncio.Semaphore(3)
        news_sem = asyncio.Semaphore(5)

        # ── Step 1: 日K報價 ──────────────────────────────────────────────────
        price_results = await asyncio.gather(*[
            _run_with_semaphore(sem, upsert_daily_prices(s["stock_code"], trade_date, s["market"]))
            for s in stocks
        ])
        price_ok = sum(price_results)
        logger.info(f"報價：成功 {price_ok}/{len(stocks)}")

        # ── Step 2: 技術指標 ─────────────────────────────────────────────────
        await asyncio.gather(*[
            _run_with_semaphore(sem, run_analysis(s["stock_code"], trade_date))
            for s in stocks
        ])

        # ── Step 3: FinMind 籌碼（含外資持股、連續天數）────────────────────
        chip_results = await asyncio.gather(*[
            _run_with_semaphore(chip_sem, upsert_chip_data(s["stock_code"], trade_date))
            for s in stocks
        ])
        chip_ok = sum(chip_results)
        logger.info(f"籌碼：成功 {chip_ok}/{len(stocks)}")

        # ── Step 4: 新聞情緒分析 ─────────────────────────────────────────────
        news_results = await asyncio.gather(*[
            _run_with_semaphore(news_sem, run_news_sentiment(s["stock_code"], trade_date))
            for s in stocks
        ])
        logger.info(f"新聞情緒：{len([r for r in news_results if r != 0])}/{len(stocks)} 有資料")

        # ── Step 5: 綜合評分 ─────────────────────────────────────────────────
        await update_composite_scores(trade_date)

        # ── Step 6: 多空篩選 ─────────────────────────────────────────────────
        candidates = await run_dynamic_screening(trade_date)

        # ── Step 7: LLM 決策（空頭候選）─────────────────────────────────────
        async def _decide_one(c):
            ind_rows = await fetch_all(
                "SELECT * FROM latest_indicators WHERE stock_code = $1", c["stock_code"]
            )
            if not ind_rows:
                return None
            rep_text = generate_text_report(c["stock_code"], ind_rows[0])
            dec = await run_cloud_decision(c["stock_code"], trade_date, rep_text)
            sc = ind_rows[0].get("composite_score")
            sc_str = f"{float(sc):.0f}" if sc else "—"
            return (
                f"{c['stock_code']} {c.get('stock_name','')} "
                f"評分:{sc_str} → *{dec.get('recommendation')}*"
            )

        decision_results = await asyncio.gather(*[
            _run_with_semaphore(llm_sem, _decide_one(c)) for c in candidates
        ])
        decisions = [d for d in decision_results if d]

        # ── Step 8: 大盤信號（含期貨）──────────────────────────────────────
        mkt = await compute_market_bear_signal(trade_date)

        # ── Step 9: Telegram 推播 ────────────────────────────────────────────
        candidate_block = (
            "\n".join([f"  • {d}" for d in decisions[:10]])
            if decisions else "  • 今日無符合條件標的"
        )
        level_emoji = {"NORMAL":"🟢","WATCH":"🔵","WARNING":"🟡","DANGER":"🟠","EXTREME":"🔴"}.get(mkt.level,"⚪")
        futures_str = f"{mkt.futures_foreign_net:+,}" if mkt.futures_foreign_net else "無資料"

        msg = (
            f"📊 *台股 AI 量化日報* `{trade_date}`\n\n"
            f"{level_emoji} *大盤風險*：`{mkt.level}`  分數 `{mkt.total_score}`\n"
            f"*法人動向*：`{mkt.institution_flow}`\n"
            f"*期貨外資淨部位*：`{futures_str}` 口\n"
            f"*操作建議*：{mkt.action}\n\n"
            f"*空頭候選（{len(candidates)} 檔）*\n{candidate_block}\n\n"
            f"_追蹤 {len(stocks)} 檔  報價 {price_ok}✓  籌碼 {chip_ok}✓_\n"
            f"_[查看完整儀表板](https://twstock-agent-1781283629.zeabur.app/dashboard)_"
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


async def _backfill_worker(days: int):
    stocks = await fetch_all("SELECT stock_code FROM stocks WHERE is_active = TRUE")
    sem = asyncio.Semaphore(6)
    results = await asyncio.gather(*[
        _run_with_semaphore(sem, backfill_prices(s["stock_code"], days))
        for s in stocks
    ])
    total = sum(results)
    logger.info(f"[Backfill] 完成，共寫入 {total} 筆歷史報價")

    latest_date = str(date.today())
    for s in stocks:
        await run_analysis(s["stock_code"], latest_date)
    logger.info("[Backfill] 技術指標重算完畢")


async def _backtest_worker(strategy: str, start_date: str, end_date: str):
    """
    回測邏輯：掃描 start~end 期間，對每日符合策略條件的股票，
    計算 5/10/20 日後報酬率，統計勝率。
    """
    from datetime import timedelta
    import json

    logger.info(f"[Backtest] {strategy} {start_date}~{end_date}")

    # 依策略設定篩選條件
    strategy_conditions = {
        "bear_alignment": "i.sma_5 < i.sma_20 AND i.sma_20 < i.sma_60",
        "double_sell":    "i.foreign_net_buy < 0 AND i.investment_trust_net_buy < 0",
        "composite_60":   "i.composite_score >= 60",
    }
    cond = strategy_conditions.get(strategy, strategy_conditions["bear_alignment"])

    signals = await fetch_all(f"""
        SELECT i.stock_code, i.trade_date::text AS signal_date
        FROM stock_indicators i
        WHERE i.trade_date BETWEEN $1::date AND $2::date
          AND {cond}
        ORDER BY i.trade_date
    """, start_date, end_date)

    if not signals:
        await execute("""
            INSERT INTO backtest_results
                (strategy_name, start_date, end_date, total_signals, win_signals,
                 win_rate, avg_return_5d, avg_return_10d, avg_return_20d, details)
            VALUES ($1,$2::date,$3::date,0,0,0,0,0,0,$4)
        """, strategy, start_date, end_date, json.dumps({"msg": "無信號"}))
        return

    returns_5d, returns_10d, returns_20d, details = [], [], [], []

    for sig in signals:
        code   = sig["stock_code"]
        sdate  = sig["signal_date"]
        td_sig = date.fromisoformat(sdate)

        prices = await fetch_all("""
            SELECT trade_date, close_price FROM stock_prices
            WHERE stock_code = $1 AND trade_date >= $2
            ORDER BY trade_date ASC LIMIT 25
        """, code, td_sig)

        if len(prices) < 2:
            continue

        entry = float(prices[0]["close_price"])
        if entry == 0:
            continue

        def get_ret(n):
            if len(prices) > n:
                return round((float(prices[n]["close_price"]) - entry) / entry * 100, 2)
            return None

        r5  = get_ret(5)
        r10 = get_ret(10)
        r20 = get_ret(20)

        if r5 is not None:
            returns_5d.append(r5)
        if r10 is not None:
            returns_10d.append(r10)
        if r20 is not None:
            returns_20d.append(r20)
        details.append({"code": code, "date": sdate, "r5d": r5, "r10d": r10, "r20d": r20})

    if not returns_5d:
        return

    # 做空策略：報酬 < 0 才算勝
    win_signals = sum(1 for r in returns_5d if r < 0)
    win_rate    = round(win_signals / len(returns_5d) * 100, 2)
    avg_5  = round(sum(returns_5d) / len(returns_5d), 2)
    avg_10 = round(sum(returns_10d) / len(returns_10d), 2) if returns_10d else 0
    avg_20 = round(sum(returns_20d) / len(returns_20d), 2) if returns_20d else 0

    cumulative = [sum(returns_5d[:i+1]) for i in range(len(returns_5d))]
    peak = cumulative[0]
    max_dd = 0.0
    for v in cumulative:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd

    await execute("""
        INSERT INTO backtest_results
            (strategy_name, start_date, end_date, total_signals, win_signals,
             win_rate, avg_return_5d, avg_return_10d, avg_return_20d,
             max_drawdown, details)
        VALUES ($1,$2::date,$3::date,$4,$5,$6,$7,$8,$9,$10,$11)
    """, strategy, start_date, end_date,
         len(returns_5d), win_signals, win_rate,
         avg_5, avg_10, avg_20, round(max_dd, 2),
         json.dumps(details[:50]))

    logger.info(f"[Backtest] 完成：{len(returns_5d)} 筆信號，勝率 {win_rate}%，5日均報酬 {avg_5}%")

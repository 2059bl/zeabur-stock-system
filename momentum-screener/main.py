"""
尾盤動量突破短線篩選系統 v1.0
================================
- 每日 22:00 自動執行 7 道過濾器篩選
- Telegram 推播結果
- REST API 供查詢與手動觸發
"""
import os
import asyncio
import logging
import datetime
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.screener import run_screening, save_results, CFG
from utils.db import get_pool, fetch_all, execute
from utils.notifier import send_message, set_webhook, get_me

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

SCHEDULE_HOUR   = int(os.environ.get("SCHEDULE_HOUR",   "22"))
SCHEDULE_MINUTE = int(os.environ.get("SCHEDULE_MINUTE", "0"))
SCHEDULE_TZ     = os.environ.get("SCHEDULE_TZ", "Asia/Taipei")

scheduler = AsyncIOScheduler(timezone=SCHEDULE_TZ)

# ── 資料庫 Schema ──────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stocks (
    stock_code         VARCHAR(10) PRIMARY KEY,
    stock_name         VARCHAR(50) NOT NULL,
    market             VARCHAR(10) NOT NULL DEFAULT 'TWSE',
    sector             VARCHAR(50),
    shares_outstanding BIGINT,
    float_shares       BIGINT,
    is_active          BOOLEAN    NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS screening_results (
    id               BIGSERIAL PRIMARY KEY,
    screen_date      DATE        NOT NULL,
    rank             INT         NOT NULL,
    stock_code       VARCHAR(10) REFERENCES stocks(stock_code),
    stock_name       VARCHAR(50),
    sector           VARCHAR(50),
    daily_return     NUMERIC(7,4),
    volume_ratio     NUMERIC(7,2),
    turnover_rate    NUMERIC(7,4),
    float_mktcap_bn  NUMERIC(10,1),
    relative_strength NUMERIC(7,4),
    rsi_14           NUMERIC(6,2),
    composite_score  NUMERIC(7,4),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(screen_date, stock_code)
);

CREATE TABLE IF NOT EXISTS workflow_logs (
    id           BIGSERIAL PRIMARY KEY,
    run_date     DATE        NOT NULL DEFAULT CURRENT_DATE,
    status       VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
    candidates   INT,
    error_msg    TEXT,
    started_at   TIMESTAMPTZ DEFAULT NOW(),
    finished_at  TIMESTAMPTZ
);
"""

# ── Telegram Webhook 處理 ──────────────────────────────────────────────────────

async def _handle_update(update: dict):
    msg  = update.get("message") or update.get("edited_message", {})
    text = (msg.get("text") or "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not text or not chat_id:
        return

    if text.startswith("/start") or text.startswith("/help"):
        await send_message(
            "📊 *尾盤動量篩選機器人*\n\n"
            "指令：\n"
            "`/today` — 今日篩選結果\n"
            "`/history` — 近 5 日紀錄\n"
            "`/run` — 立即觸發篩選\n"
            "`/stocks` — 追蹤股票清單\n"
            "`/config` — 目前篩選參數",
            chat_id=chat_id,
        )

    elif text.startswith("/today"):
        rows = await fetch_all("""
            SELECT rank, stock_code, stock_name, daily_return,
                   volume_ratio, composite_score, screen_date
            FROM screening_results
            WHERE screen_date = (SELECT MAX(screen_date) FROM screening_results)
            ORDER BY rank ASC
        """)
        if not rows:
            await send_message("目前無篩選資料，請稍後或執行 /run", chat_id=chat_id)
            return
        date_str = rows[0]["screen_date"]
        lines = [f"📈 *尾盤動量候選股 {date_str}*\n"]
        for r in rows:
            ret_pct = f"{float(r['daily_return'])*100:.1f}%"
            vr      = f"{float(r['volume_ratio']):.1f}x"
            sc      = f"{float(r['composite_score']):.3f}"
            lines.append(
                f"#{r['rank']} `{r['stock_code']}` {r['stock_name']}\n"
                f"    漲幅 {ret_pct}  量比 {vr}  評分 {sc}"
            )
        await send_message("\n".join(lines), chat_id=chat_id)

    elif text.startswith("/history"):
        rows = await fetch_all("""
            SELECT screen_date, COUNT(*) AS cnt,
                   MAX(composite_score) AS top_score
            FROM screening_results
            GROUP BY screen_date
            ORDER BY screen_date DESC
            LIMIT 5
        """)
        if not rows:
            await send_message("無歷史紀錄", chat_id=chat_id)
            return
        lines = ["📅 *近期篩選紀錄*\n"]
        for r in rows:
            lines.append(f"{r['screen_date']}  {r['cnt']} 檔  最高分 {float(r['top_score']):.3f}")
        await send_message("\n".join(lines), chat_id=chat_id)

    elif text.startswith("/run"):
        today = datetime.date.today()
        await send_message(f"⚙️ 已觸發 {today} 篩選，約 1 分鐘後推播結果", chat_id=chat_id)
        asyncio.create_task(_screening_worker(today))

    elif text.startswith("/stocks"):
        rows = await fetch_all(
            "SELECT stock_code, stock_name, market FROM stocks WHERE is_active=TRUE ORDER BY market, stock_code"
        )
        if not rows:
            await send_message("股票池為空，請透過 API 新增", chat_id=chat_id)
            return
        lines = [f"📋 *追蹤股票池（{len(rows)} 檔）*\n"]
        twse = [r for r in rows if r["market"] == "TWSE"]
        otc  = [r for r in rows if r["market"] == "OTC"]
        if twse:
            lines.append("*上市（TWSE）*")
            lines.extend(f"`{r['stock_code']}` {r['stock_name']}" for r in twse)
        if otc:
            lines.append("\n*上櫃（OTC）*")
            lines.extend(f"`{r['stock_code']}` {r['stock_name']}" for r in otc)
        await send_message("\n".join(lines), chat_id=chat_id)

    elif text.startswith("/config"):
        lines = ["⚙️ *目前篩選參數*\n"]
        param_names = {
            "return_min":         "最低漲幅",
            "return_max":         "最高漲幅",
            "vol_ratio_min":      "量比門檻",
            "turnover_min":       "最低換手率",
            "turnover_max":       "最高換手率",
            "mktcap_min":         "最低市值(億)",
            "mktcap_max":         "最高市值(億)",
            "vol_slope_tolerance":"量能容差",
            "ma_slope_min":       "均線斜率門檻",
            "rsi_max":            "RSI 上限",
            "top_n":              "輸出檔數",
        }
        for k, label in param_names.items():
            val = CFG.get(k)
            if k in ("mktcap_min", "mktcap_max"):
                val = f"{val/1e9:.0f}億"
            elif k in ("return_min","return_max","turnover_min","turnover_max","ma_slope_min","vol_slope_tolerance"):
                val = f"{val*100:.1f}%"
            lines.append(f"`{label}`: {val}")
        await send_message("\n".join(lines), chat_id=chat_id)

    else:
        await send_message(
            "❓ 未知指令。輸入 /help 查看可用指令。",
            chat_id=chat_id,
        )


# ── 篩選工作器 ────────────────────────────────────────────────────────────────

async def _screening_worker(trade_date: datetime.date):
    log_id = None
    try:
        res = await execute(
            "INSERT INTO workflow_logs (run_date, status) VALUES ($1, 'RUNNING') RETURNING id",
            trade_date,
        )
        # asyncpg execute 不回傳 RETURNING，改用 fetch_all
        row = await fetch_all(
            "INSERT INTO workflow_logs (run_date, status) VALUES ($1, 'RUNNING') RETURNING id",
            trade_date,
        )
        log_id = row[0]["id"] if row else None
    except Exception:
        pass

    try:
        candidates = await run_screening(trade_date)
        await save_results(candidates)

        if log_id:
            await execute(
                "UPDATE workflow_logs SET status='SUCCESS', candidates=$2, finished_at=NOW() WHERE id=$1",
                log_id, len(candidates),
            )

        # Telegram 推播
        if not candidates:
            msg = f"📊 *{trade_date} 尾盤動量篩選*\n今日無符合條件標的（市場動能不足）"
        else:
            lines = [f"📈 *尾盤動量候選股 {trade_date}*（7道過濾器）\n"]
            for r in candidates:
                ret_pct  = f"{r['daily_return']*100:.1f}%"
                vr       = f"{r['volume_ratio']:.1f}x"
                mktcap   = f"{r['float_mktcap_bn']:.0f}億" if r["float_mktcap_bn"] else "—"
                rsi      = f"RSI {r['rsi_14']:.0f}" if r["rsi_14"] else ""
                lines.append(
                    f"#{candidates.index(r)+1} `{r['stock_code']}` {r['stock_name']}\n"
                    f"    漲幅{ret_pct} 量比{vr} 市值{mktcap} {rsi}"
                )
            lines.append(f"\n_評分依據：量比40% + 相對強度35% + 換手25%_")
            msg = "\n".join(lines)

        await send_message(msg)
        logger.info(f"[{trade_date}] 篩選完成：{len(candidates)} 檔候選")

    except Exception as e:
        logger.exception(f"篩選失敗: {e}")
        if log_id:
            safe = str(e)[:500]
            await execute(
                "UPDATE workflow_logs SET status='FAILED', error_msg=$2, finished_at=NOW() WHERE id=$1",
                log_id, safe,
            )


async def _scheduled_run():
    today = datetime.date.today()
    logger.info(f"[排程] 22:00 自動觸發篩選：{today}")
    await _screening_worker(today)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    # 建立 Schema
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
    logger.info("Schema 初始化完成")

    scheduler.add_job(
        _scheduled_run,
        CronTrigger(hour=SCHEDULE_HOUR, minute=SCHEDULE_MINUTE, timezone=SCHEDULE_TZ),
        id="daily_screen",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"排程啟動：每日 {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} {SCHEDULE_TZ}")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="尾盤動量篩選系統", version="1.0.0", lifespan=lifespan)


# ── Models ────────────────────────────────────────────────────────────────────

class StockAdd(BaseModel):
    stock_code:         str
    stock_name:         str
    market:             str   = "TWSE"
    sector:             Optional[str] = None
    shares_outstanding: Optional[int] = None
    float_shares:       Optional[int] = None


class RunRequest(BaseModel):
    trade_date: Optional[str] = None


# ── API Routes ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    stocks = await fetch_all("SELECT COUNT(*) AS n FROM stocks WHERE is_active=TRUE")
    return {
        "status":   "ok",
        "version":  "1.0.0",
        "time":     datetime.datetime.utcnow().isoformat(),
        "schedule": f"{SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} {SCHEDULE_TZ}",
        "tracking": stocks[0]["n"] if stocks else 0,
    }


@app.get("/stocks")
async def list_stocks():
    return await fetch_all(
        "SELECT * FROM stocks WHERE is_active=TRUE ORDER BY market, stock_code"
    )


@app.post("/stocks")
async def add_stock(s: StockAdd):
    await execute("""
        INSERT INTO stocks
            (stock_code, stock_name, market, sector, shares_outstanding, float_shares)
        VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (stock_code) DO UPDATE SET
            stock_name         = EXCLUDED.stock_name,
            is_active          = TRUE,
            sector             = COALESCE(EXCLUDED.sector, stocks.sector),
            shares_outstanding = COALESCE(EXCLUDED.shares_outstanding, stocks.shares_outstanding),
            float_shares       = COALESCE(EXCLUDED.float_shares, stocks.float_shares)
    """, s.stock_code, s.stock_name, s.market, s.sector,
         s.shares_outstanding, s.float_shares)
    return {"status": "ok", "stock_code": s.stock_code}


@app.delete("/stocks/{stock_code}")
async def remove_stock(stock_code: str):
    await execute("UPDATE stocks SET is_active=FALSE WHERE stock_code=$1", stock_code)
    return {"status": "ok"}


@app.post("/run")
async def trigger_run(req: RunRequest, background_tasks: BackgroundTasks):
    td = datetime.date.fromisoformat(req.trade_date) if req.trade_date else datetime.date.today()
    background_tasks.add_task(_screening_worker, td)
    return {"status": "已排入執行", "date": str(td)}


@app.get("/results")
async def get_results(
    screen_date: Optional[str] = Query(None, description="YYYY-MM-DD，預設最新一日"),
    limit: int = Query(10, le=50),
):
    if screen_date:
        td = datetime.date.fromisoformat(screen_date)
    else:
        rows = await fetch_all("SELECT MAX(screen_date) AS d FROM screening_results")
        td = rows[0]["d"] if rows and rows[0]["d"] else datetime.date.today()

    return await fetch_all("""
        SELECT rank, stock_code, stock_name, sector,
               daily_return, volume_ratio, turnover_rate,
               float_mktcap_bn, relative_strength, rsi_14,
               composite_score, screen_date
        FROM screening_results
        WHERE screen_date = $1
        ORDER BY rank ASC
        LIMIT $2
    """, td, limit)


@app.get("/results/history")
async def results_history(days: int = Query(10, le=30)):
    return await fetch_all("""
        SELECT screen_date, COUNT(*) AS candidates,
               ROUND(AVG(composite_score)::numeric, 3) AS avg_score,
               ROUND(MAX(composite_score)::numeric, 3) AS top_score
        FROM screening_results
        GROUP BY screen_date
        ORDER BY screen_date DESC
        LIMIT $1
    """, days)


@app.get("/logs")
async def workflow_logs():
    return await fetch_all(
        "SELECT * FROM workflow_logs ORDER BY started_at DESC LIMIT 20"
    )


@app.get("/config")
async def get_config():
    return CFG


@app.post("/telegram/webhook")
async def telegram_webhook(update: dict, background_tasks: BackgroundTasks):
    background_tasks.add_task(_handle_update, update)
    return {"ok": True}


@app.post("/setup/webhook")
async def setup_webhook(url: str = Query(..., description="https://你的域名/telegram/webhook")):
    result = await set_webhook(url)
    return result


@app.get("/setup/bot")
async def bot_info():
    return await get_me()


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    results = await fetch_all("""
        SELECT rank, stock_code, stock_name, sector,
               ROUND((daily_return*100)::numeric,2) AS ret_pct,
               ROUND(volume_ratio::numeric,2) AS vr,
               ROUND((turnover_rate*100)::numeric,2) AS tr_pct,
               ROUND(float_mktcap_bn::numeric,1) AS mktcap,
               ROUND((relative_strength*100)::numeric,2) AS rs_pct,
               ROUND(rsi_14::numeric,1) AS rsi,
               ROUND(composite_score::numeric,3) AS score,
               screen_date
        FROM screening_results
        WHERE screen_date = (SELECT MAX(screen_date) FROM screening_results)
        ORDER BY rank ASC
    """)
    logs = await fetch_all(
        "SELECT run_date, status, candidates, error_msg, started_at FROM workflow_logs ORDER BY started_at DESC LIMIT 5"
    )

    date_str = str(results[0]["screen_date"]) if results else "尚無資料"

    rows_html = ""
    for r in results:
        rsi_color = "#ef4444" if r["rsi"] and float(r["rsi"]) > 65 else "#22c55e"
        rows_html += f"""
        <tr>
          <td>{r['rank']}</td>
          <td><strong>{r['stock_code']}</strong></td>
          <td>{r['stock_name']}</td>
          <td>{r['sector'] or '—'}</td>
          <td style="color:#22c55e">+{r['ret_pct']}%</td>
          <td>{r['vr']}×</td>
          <td>{r['tr_pct'] or '—'}%</td>
          <td>{r['mktcap'] or '—'}億</td>
          <td>+{r['rs_pct']}%</td>
          <td style="color:{rsi_color}">{r['rsi'] or '—'}</td>
          <td><strong>{r['score']}</strong></td>
        </tr>"""

    log_html = ""
    for l in logs:
        color = "#22c55e" if l["status"] == "SUCCESS" else "#ef4444" if l["status"] == "FAILED" else "#f59e0b"
        log_html += f"<tr><td>{l['run_date']}</td><td style='color:{color}'>{l['status']}</td><td>{l['candidates'] or '—'}</td><td style='font-size:11px'>{l['error_msg'] or ''}</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>尾盤動量篩選系統</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ background:#0f172a; color:#e2e8f0; font-family:'Noto Sans TC',sans-serif; padding:24px }}
  h1 {{ font-size:22px; color:#38bdf8; margin-bottom:4px }}
  .subtitle {{ color:#64748b; font-size:13px; margin-bottom:24px }}
  .card {{ background:#1e293b; border-radius:12px; padding:20px; margin-bottom:20px }}
  table {{ width:100%; border-collapse:collapse; font-size:13px }}
  th {{ background:#0f172a; color:#94a3b8; padding:10px 8px; text-align:left; position:sticky; top:0 }}
  td {{ padding:9px 8px; border-bottom:1px solid #334155 }}
  tr:hover td {{ background:#263047 }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px }}
  .ok {{ background:#166534; color:#86efac }}
  .fail {{ background:#7f1d1d; color:#fca5a5 }}
</style>
</head>
<body>
<h1>📈 尾盤動量突破篩選系統</h1>
<p class="subtitle">策略：7道過濾器 ｜ 排程：每日 22:00 Asia/Taipei ｜ 最新篩選：{date_str}</p>

<div class="card">
  <h2 style="font-size:15px;color:#94a3b8;margin-bottom:12px">候選股（{len(results)} 檔）</h2>
  <div style="overflow-x:auto">
  <table>
    <thead><tr>
      <th>#</th><th>代碼</th><th>名稱</th><th>類股</th>
      <th>漲幅</th><th>量比</th><th>換手率</th><th>市值</th>
      <th>相對強度</th><th>RSI</th><th>綜合分</th>
    </tr></thead>
    <tbody>{rows_html or '<tr><td colspan="11" style="text-align:center;padding:30px;color:#64748b">尚無篩選資料</td></tr>'}</tbody>
  </table>
  </div>
</div>

<div class="card">
  <h2 style="font-size:15px;color:#94a3b8;margin-bottom:12px">執行紀錄</h2>
  <table>
    <thead><tr><th>日期</th><th>狀態</th><th>候選數</th><th>錯誤</th></tr></thead>
    <tbody>{log_html or '<tr><td colspan="4" style="text-align:center;padding:20px;color:#64748b">尚無執行紀錄</td></tr>'}</tbody>
  </table>
</div>
</body></html>"""

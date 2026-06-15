"""
尾盤動量突破短線篩選系統 v2.0
================================
- 每日 22:00 自動執行 7 道過濾器篩選
- 三大法人籌碼追蹤（外資/投信/自營）
- 外資持股比例（FinMind）
- 即時報價與漲跌幅（富果/Yahoo）
- Telegram 智能客服聊天 + 指令機器人
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
from utils.institutional import fetch_institutional_flows, fetch_foreign_shareholding
from utils.fugle import fetch_all_quotes, fetch_quote

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

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama-inference.zeabur.app")

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

CREATE TABLE IF NOT EXISTS institutional_daily (
    id            BIGSERIAL PRIMARY KEY,
    stock_code    VARCHAR(10) NOT NULL,
    trade_date    DATE        NOT NULL,
    foreign_buy   BIGINT      DEFAULT 0,
    foreign_sell  BIGINT      DEFAULT 0,
    foreign_net   BIGINT      DEFAULT 0,
    trust_net     BIGINT      DEFAULT 0,
    dealer_net    BIGINT      DEFAULT 0,
    total_net     BIGINT      DEFAULT 0,
    foreign_consec INT        DEFAULT 0,
    foreign_ratio  NUMERIC(6,2),
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(stock_code, trade_date)
);
"""

# ── Telegram Webhook 處理 ──────────────────────────────────────────────────────

async def _ollama_chat(user_message: str) -> str:
    """呼叫 Ollama LLM 回應自然語言問題。"""
    import httpx
    system_prompt = (
        "你是一位專業的台股量化選股助理，負責解說「尾盤動量突破短線篩選系統」的功能與結果。\n"
        "你熟悉技術分析（MA均線、RSI、量比、換手率）、三大法人籌碼（外資/投信/自營商）、\n"
        "以及台灣股市的基本知識。\n"
        "回答請用繁體中文，簡潔專業，必要時使用條列式說明。\n"
        "若問題超出股票範疇，禮貌說明你專注於股市分析。"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{OLLAMA_HOST}/api/chat",
                json={
                    "model": "llama3.1:8b",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_message},
                    ],
                    "stream": False,
                },
            )
            if r.status_code == 200:
                return r.json().get("message", {}).get("content", "")
    except Exception as e:
        logger.warning(f"Ollama chat error: {e}")

    # Fallback: 基本關鍵字回應
    msg_lower = user_message.lower()
    if any(k in msg_lower for k in ["外資", "法人", "籌碼"]):
        return "請用 `/foreign 股票代號` 查詢外資籌碼，例如 `/foreign 2330`"
    if any(k in msg_lower for k in ["報價", "漲跌", "今天"]):
        return "請用 `/quotes` 查詢所有追蹤股今日報價，或 `/q 股票代號` 查詢單一股票"
    if any(k in msg_lower for k in ["篩選", "選股", "結果"]):
        return "請用 `/today` 查看最新篩選結果，或 `/run` 立即觸發篩選"
    return "您好！我是台股動量篩選助理 📊\n輸入 /help 查看所有指令，或直接問我問題。"


async def _handle_update(update: dict):
    msg  = update.get("message") or update.get("edited_message", {})
    text = (msg.get("text") or "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not text or not chat_id:
        return

    if text.startswith("/start") or text.startswith("/help"):
        await send_message(
            "📊 *尾盤動量篩選機器人 v2.0*\n\n"
            "*📈 篩選功能*\n"
            "`/today` — 今日篩選結果\n"
            "`/history` — 近 5 日紀錄\n"
            "`/run` — 立即觸發篩選\n\n"
            "*💰 報價功能*\n"
            "`/quotes` — 所有追蹤股今日漲跌\n"
            "`/q 代號` — 查單一股票（如 `/q 2330`）\n\n"
            "*🏦 法人籌碼*\n"
            "`/foreign 代號` — 外資買賣超+持股比例\n"
            "`/institutional 代號` — 三大法人明細\n\n"
            "*⚙️ 系統功能*\n"
            "`/stocks` — 追蹤股票清單\n"
            "`/config` — 篩選參數\n\n"
            "💬 也可以直接輸入問題，我會盡力回答！",
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

    elif text.startswith("/quotes"):
        rows = await fetch_all(
            "SELECT stock_code, stock_name FROM stocks WHERE is_active=TRUE ORDER BY stock_code"
        )
        if not rows:
            await send_message("股票池為空", chat_id=chat_id)
            return
        await send_message("⏳ 抓取報價中...", chat_id=chat_id)
        codes = [r["stock_code"] for r in rows]
        name_map = {r["stock_code"]: r["stock_name"] for r in rows}
        quotes = await fetch_all_quotes(codes)

        up   = [(c, q) for c, q in quotes.items() if q["change_pct"] > 0]
        down = [(c, q) for c, q in quotes.items() if q["change_pct"] < 0]
        flat = [(c, q) for c, q in quotes.items() if q["change_pct"] == 0]
        up.sort(key=lambda x: x[1]["change_pct"], reverse=True)
        down.sort(key=lambda x: x[1]["change_pct"])

        lines = [f"📊 *追蹤股今日報價*（{len(quotes)}/{len(codes)} 筆）\n"]
        if up:
            lines.append("🟢 *上漲*")
            for c, q in up[:10]:
                lines.append(f"`{c}` {name_map.get(c,'')} {q['close']} *+{q['change_pct']:.1f}%* 量{q['volume']}張")
        if down:
            lines.append("\n🔴 *下跌*")
            for c, q in down[:10]:
                lines.append(f"`{c}` {name_map.get(c,'')} {q['close']} *{q['change_pct']:.1f}%* 量{q['volume']}張")
        if flat:
            lines.append(f"\n⬜ 平盤：{len(flat)} 檔")
        await send_message("\n".join(lines), chat_id=chat_id)

    elif text.startswith("/q "):
        code = text.split()[1].strip()
        q = await fetch_quote(code)
        rows = await fetch_all("SELECT stock_name FROM stocks WHERE stock_code=$1", code)
        name = rows[0]["stock_name"] if rows else ""
        if not q:
            await send_message(f"❌ 找不到 {code} 的報價", chat_id=chat_id)
            return
        sign = "+" if q["change_pct"] >= 0 else ""
        emoji = "🟢" if q["change_pct"] > 0 else ("🔴" if q["change_pct"] < 0 else "⬜")
        await send_message(
            f"{emoji} *{code} {name or q.get('name','')}*\n"
            f"現價：*{q['close']}*  {sign}{q['change']:.2f} ({sign}{q['change_pct']:.2f}%)\n"
            f"開高低：{q['open']} / {q['high']} / {q['low']}\n"
            f"成交量：{q['volume']} 張",
            chat_id=chat_id,
        )

    elif text.startswith("/foreign"):
        parts = text.split()
        if len(parts) < 2:
            await send_message("用法：`/foreign 股票代號`，例如 `/foreign 2330`", chat_id=chat_id)
            return
        code = parts[1].strip()
        _tz  = datetime.timezone(datetime.timedelta(hours=8))
        td   = datetime.datetime.now(_tz).date()
        await send_message(f"⏳ 查詢 {code} 外資資料...", chat_id=chat_id)
        flows  = await fetch_institutional_flows(code, td)
        holding = await fetch_foreign_shareholding(code)

        net = flows.get("foreign_net", 0)
        consec = flows.get("foreign_consec", 0)
        ratio  = holding.get("foreign_ratio", "N/A")
        consec_str = (f"連買 {consec} 日 📈" if consec > 0
                      else (f"連賣 {abs(consec)} 日 📉" if consec < 0 else "持平"))
        net_str = f"+{net:,}" if net >= 0 else f"{net:,}"

        await send_message(
            f"🏦 *{code} 外資籌碼*（{td}）\n\n"
            f"外資買超：{flows.get('foreign_buy',0):,} 張\n"
            f"外資賣超：{flows.get('foreign_sell',0):,} 張\n"
            f"外資買賣超：*{net_str} 張*\n"
            f"外資動向：{consec_str}\n\n"
            f"投信買賣超：{flows.get('trust_net',0):,} 張\n"
            f"自營商買賣超：{flows.get('dealer_net',0):,} 張\n"
            f"三大法人合計：{flows.get('total_net',0):,} 張\n\n"
            f"外資持股比例：*{ratio}%*\n"
            f"（資料日期：{holding.get('date','N/A')}）",
            chat_id=chat_id,
        )

    elif text.startswith("/institutional"):
        parts = text.split()
        if len(parts) < 2:
            await send_message("用法：`/institutional 股票代號`", chat_id=chat_id)
            return
        code = parts[1].strip()
        _tz  = datetime.timezone(datetime.timedelta(hours=8))
        td   = datetime.datetime.now(_tz).date()
        flows = await fetch_institutional_flows(code, td)
        total = flows.get("total_net", 0)
        sign  = "+" if total >= 0 else ""
        await send_message(
            f"📋 *{code} 三大法人買賣超*（{td}）\n\n"
            f"外資：*{'+' if flows.get('foreign_net',0)>=0 else ''}{flows.get('foreign_net',0):,}* 張\n"
            f"投信：*{'+' if flows.get('trust_net',0)>=0 else ''}{flows.get('trust_net',0):,}* 張\n"
            f"自營：*{'+' if flows.get('dealer_net',0)>=0 else ''}{flows.get('dealer_net',0):,}* 張\n"
            f"─────────────\n"
            f"合計：*{sign}{total:,}* 張\n"
            f"外資連續：{flows.get('foreign_consec',0)} 日",
            chat_id=chat_id,
        )

    elif text.startswith("/run"):
        _tz_taipei = datetime.timezone(datetime.timedelta(hours=8))
        today = datetime.datetime.now(_tz_taipei).date()
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
        # 自然語言聊天（Ollama LLM）
        reply = await _ollama_chat(text)
        await send_message(reply, chat_id=chat_id)


# ── 篩選工作器 ────────────────────────────────────────────────────────────────

async def _screening_worker(trade_date: datetime.date):
    log_id = None
    try:
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


async def _update_institutional_cache(trade_date: datetime.date):
    """批量更新所有追蹤股的法人資料到 DB 快取。"""
    stocks = await fetch_all("SELECT stock_code FROM stocks WHERE is_active=TRUE")
    logger.info(f"[法人] 開始更新 {len(stocks)} 檔法人資料")
    sem = asyncio.Semaphore(5)

    async def _one(code):
        async with sem:
            try:
                flows   = await fetch_institutional_flows(code, trade_date)
                holding = await fetch_foreign_shareholding(code)
                await execute("""
                    INSERT INTO institutional_daily
                        (stock_code, trade_date, foreign_buy, foreign_sell,
                         foreign_net, trust_net, dealer_net, total_net,
                         foreign_consec, foreign_ratio)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    ON CONFLICT (stock_code, trade_date) DO UPDATE SET
                        foreign_buy   = EXCLUDED.foreign_buy,
                        foreign_sell  = EXCLUDED.foreign_sell,
                        foreign_net   = EXCLUDED.foreign_net,
                        trust_net     = EXCLUDED.trust_net,
                        dealer_net    = EXCLUDED.dealer_net,
                        total_net     = EXCLUDED.total_net,
                        foreign_consec = EXCLUDED.foreign_consec,
                        foreign_ratio = EXCLUDED.foreign_ratio,
                        updated_at    = NOW()
                """,
                code, trade_date,
                flows.get("foreign_buy", 0),
                flows.get("foreign_sell", 0),
                flows.get("foreign_net", 0),
                flows.get("trust_net", 0),
                flows.get("dealer_net", 0),
                flows.get("total_net", 0),
                flows.get("foreign_consec", 0),
                holding.get("foreign_ratio"),
                )
            except Exception as e:
                logger.warning(f"[法人] {code} 更新失敗: {e}")

    await asyncio.gather(*[_one(r["stock_code"]) for r in stocks])
    logger.info(f"[法人] {trade_date} 法人資料更新完成")


async def _scheduled_run():
    _tz_taipei = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(_tz_taipei).date()
    logger.info(f"[排程] 22:00 自動觸發篩選：{today}")
    # 並行執行篩選 + 法人資料更新
    await asyncio.gather(
        _screening_worker(today),
        _update_institutional_cache(today),
    )


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
    _tz_taipei = datetime.timezone(datetime.timedelta(hours=8))
    td = datetime.date.fromisoformat(req.trade_date) if req.trade_date else datetime.datetime.now(_tz_taipei).date()
    background_tasks.add_task(_screening_worker, td)
    return {"status": "已排入執行", "date": str(td)}


@app.post("/institutional/refresh")
async def refresh_institutional(
    background_tasks: BackgroundTasks,
    trade_date: Optional[str] = Query(None),
):
    """手動觸發法人資料更新。"""
    _tz = datetime.timezone(datetime.timedelta(hours=8))
    td  = (datetime.date.fromisoformat(trade_date)
           if trade_date else datetime.datetime.now(_tz).date())
    background_tasks.add_task(_update_institutional_cache, td)
    return {"status": "已排入更新", "date": str(td)}


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


# ── 報價端點 ──────────────────────────────────────────────────────────────────

@app.get("/quotes")
async def stocks_quotes():
    """所有追蹤股當日報價、漲跌幅、成交量。"""
    rows = await fetch_all(
        "SELECT stock_code, stock_name, market FROM stocks WHERE is_active=TRUE ORDER BY stock_code"
    )
    if not rows:
        return []
    codes    = [r["stock_code"] for r in rows]
    name_map = {r["stock_code"]: r["stock_name"] for r in rows}
    mkt_map  = {r["stock_code"]: r["market"]     for r in rows}
    quotes   = await fetch_all_quotes(codes)

    result = []
    for code in codes:
        q = quotes.get(code)
        if q:
            result.append({
                "stock_code":  code,
                "stock_name":  name_map.get(code, ""),
                "market":      mkt_map.get(code, ""),
                **q,
            })
        else:
            result.append({
                "stock_code": code,
                "stock_name": name_map.get(code, ""),
                "market":     mkt_map.get(code, ""),
                "close": None, "change_pct": None, "volume": None,
            })
    return result


@app.get("/quote/{stock_code}")
async def stock_quote(stock_code: str):
    """單一股票即時報價。"""
    return await fetch_quote(stock_code)


# ── 法人籌碼端點 ──────────────────────────────────────────────────────────────

@app.get("/institutional/summary")
async def institutional_summary(
    trade_date: Optional[str] = Query(None),
    limit: int = Query(20, le=53),
):
    """所有追蹤股的法人籌碼摘要（從 DB 快取取）。"""
    _tz = datetime.timezone(datetime.timedelta(hours=8))
    td  = (datetime.date.fromisoformat(trade_date)
           if trade_date else datetime.datetime.now(_tz).date())
    return await fetch_all("""
        SELECT i.stock_code, s.stock_name,
               i.foreign_net, i.trust_net, i.dealer_net, i.total_net,
               i.foreign_consec, i.foreign_ratio, i.trade_date
        FROM institutional_daily i
        JOIN stocks s ON s.stock_code = i.stock_code
        WHERE i.trade_date = $1 AND s.is_active = TRUE
        ORDER BY i.total_net DESC
        LIMIT $2
    """, td, limit)


@app.get("/institutional/{stock_code}")
async def institutional_data(
    stock_code: str,
    trade_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
):
    """三大法人買賣超（外資/投信/自營）。"""
    _tz = datetime.timezone(datetime.timedelta(hours=8))
    td  = (datetime.date.fromisoformat(trade_date)
           if trade_date else datetime.datetime.now(_tz).date())
    flows = await fetch_institutional_flows(stock_code, td)
    return {"stock_code": stock_code, "trade_date": str(td), **flows}


@app.get("/foreign-holding/{stock_code}")
async def foreign_holding(stock_code: str):
    """外資持股比例（%）。"""
    return await fetch_foreign_shareholding(stock_code)


# ── 診斷端點 ──────────────────────────────────────────────────────────────────

@app.get("/debug/screen")
async def debug_screen(
    trade_date: Optional[str] = Query(None),
    stock_code: Optional[str] = Query(None, description="指定股票代號，空白則全部"),
):
    """逐 filter 診斷，回傳每檔股票在哪個 Step 被過濾掉。"""
    from utils.price_fetcher import fetch_stock_data, fetch_market_return
    from agents.screener import CFG, _rsi

    _tz = datetime.timezone(datetime.timedelta(hours=8))
    td = datetime.date.fromisoformat(trade_date) if trade_date else datetime.datetime.now(_tz).date()

    stocks = await fetch_all(
        "SELECT stock_code, stock_name, market, sector, shares_outstanding, float_shares "
        "FROM stocks WHERE is_active=TRUE" + (" AND stock_code=$1" if stock_code else ""),
        *([stock_code] if stock_code else [])
    )

    market_return = await fetch_market_return(td)

    import asyncio
    sem = asyncio.Semaphore(5)
    async def _fetch(s):
        async with sem:
            return s, await fetch_stock_data(s["stock_code"])

    raw = await asyncio.gather(*[_fetch(s) for s in stocks])

    report = []
    for stock, data in raw:
        code = stock["stock_code"]
        name = stock["stock_name"]

        if data is None:
            report.append({"code": code, "name": name, "fail": "no_data", "data": None})
            continue

        ret        = data["daily_return"]
        vol_ratio  = data["volume_ratio"]
        closes     = data["closes"]
        vols_3d    = data["volumes_3d"]
        price      = data["close"]
        ma5, ma10, ma20, ma60 = data["ma5"], data["ma10"], data["ma20"], data["ma60"]
        float_sh   = stock.get("float_shares") or stock.get("shares_outstanding")
        volume_today = data["volume_today"]
        turnover   = ((volume_today * 1000) / float_sh) if (float_sh and float_sh > 0 and volume_today > 0) else None
        float_mktcap = price * (float_sh or 0)
        rsi_val    = _rsi(closes)

        fail = None
        if not (CFG["return_min"] <= ret <= CFG["return_max"]):
            fail = f"step1_return({ret*100:.2f}%,要3-5%)"
        elif vol_ratio is None or vol_ratio < CFG["vol_ratio_min"]:
            fail = f"step2_vol_ratio({vol_ratio},要>{CFG['vol_ratio_min']})"
        elif turnover is not None and not (CFG["turnover_min"] <= turnover <= CFG["turnover_max"]):
            fail = f"step3_turnover({turnover*100:.2f}%,要5-10%)"
        elif float_sh and not (CFG["mktcap_min"] <= float_mktcap <= CFG["mktcap_max"]):
            fail = f"step4_mktcap({float_mktcap/1e9:.0f}億,要250-2500億)"
        elif len(vols_3d) == 3:
            v0, v1, v2 = vols_3d
            tol = CFG["vol_slope_tolerance"]
            if not ((v1 >= v0*(1-tol)) and (v2 >= v1*(1-tol)) and (v2 > v0)):
                fail = f"step5_vol_slope(vols={v0},{v1},{v2})"
        if fail is None and None in (ma5, ma10, ma20):
            fail = f"step6a_ma_none(ma5={ma5},ma10={ma10},ma20={ma20})"
        elif fail is None and not (price > ma5 > ma10 > ma20):
            fail = f"step6a_ma_order(p={price:.1f}>ma5={ma5:.1f}>ma10={ma10:.1f}>ma20={ma20:.1f}?)"
        elif fail is None and (ma60 and ma20 <= ma60):
            fail = f"step6a_ma60(ma20={ma20:.1f}<=ma60={ma60:.1f})"
        elif fail is None:
            slopes_ok = all(s is not None and s > CFG["ma_slope_min"]
                            for s in (data["ma5_slope"], data["ma10_slope"], data["ma20_slope"]))
            if not slopes_ok:
                fail = f"step6b_slope({data['ma5_slope']:.4f},{data['ma10_slope']:.4f},{data['ma20_slope']:.4f})"
        if fail is None and rsi_val is not None and rsi_val >= CFG["rsi_max"]:
            fail = f"step6c_rsi({rsi_val:.1f}>={CFG['rsi_max']})"
        if fail is None:
            rel = ret - market_return
            if rel <= 0:
                fail = f"step7_rel_strength({rel*100:.2f}%<=0)"

        report.append({
            "code": code, "name": name,
            "fail": fail or "PASS",
            "ret_pct": round(ret*100, 2),
            "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
            "mktcap_bn": round(float_mktcap/1e9, 1) if float_sh else None,
            "rsi": round(rsi_val, 1) if rsi_val else None,
            "data_days": len(closes),
            "ma5": round(ma5,2) if ma5 else None,
            "ma20": round(ma20,2) if ma20 else None,
        })

    passed  = [r for r in report if r["fail"] == "PASS"]
    by_step = {}
    for r in report:
        if r["fail"] != "PASS":
            step = r["fail"].split("(")[0]
            by_step[step] = by_step.get(step, 0) + 1

    return {
        "trade_date": str(td),
        "market_return_pct": round(market_return*100, 2),
        "total": len(report),
        "passed": len(passed),
        "filter_stats": by_step,
        "details": sorted(report, key=lambda x: (x["fail"] != "PASS", x["fail"])),
    }


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

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
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.screener import run_screening, save_results, CFG
from utils.db import get_pool, fetch_all, execute
from utils.notifier import send_message, send_photo, set_webhook, get_me
from utils.institutional import (
    fetch_institutional_flows, fetch_foreign_shareholding, fetch_foreign_ratio_trend,
)
from utils.fugle import fetch_all_quotes, fetch_quote
from utils.kline import generate_kline_chart
from utils.dividend import fetch_upcoming_exdiv

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
            "📊 *尾盤動量篩選機器人 v2.2*\n\n"
            "*📈 篩選功能*\n"
            "`/today` — 今日篩選結果\n"
            "`/history` — 近 5 日紀錄\n"
            "`/run` — 立即觸發篩選\n\n"
            "*💰 報價功能*\n"
            "`/quotes` — 所有追蹤股今日漲跌\n"
            "`/q 代號` — 查單一股票 + K 線圖（如 `/q 2330`）\n\n"
            "*🏦 法人籌碼*\n"
            "`/foreign 代號` — 外資買賣超 + 持股比例趨勢\n"
            "`/institutional 代號` — 三大法人明細\n\n"
            "*📅 除權息*\n"
            "`/exdiv` — 追蹤股未來 7 日除權息清單\n\n"
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
        sign  = "+" if q["change_pct"] >= 0 else ""
        emoji = "🟢" if q["change_pct"] > 0 else ("🔴" if q["change_pct"] < 0 else "⬜")
        caption = (
            f"{emoji} *{code} {name or q.get('name','')}*\n"
            f"現價：*{q['close']}*  {sign}{q['change']:.2f} ({sign}{q['change_pct']:.2f}%)\n"
            f"開高低：{q['open']} / {q['high']} / {q['low']}\n"
            f"成交量：{q['volume']} 張"
        )
        # 嘗試傳送 K 線圖（含報價 caption）
        chart = await generate_kline_chart(code, name or q.get("name", ""), days=10)
        if chart:
            await send_photo(chart, caption=caption, chat_id=chat_id)
        else:
            await send_message(caption, chat_id=chat_id)

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

        # 外資持股比例趨勢
        trend_data = await fetch_foreign_ratio_trend(code, days=5)
        r_days = trend_data.get("rising_days", 0)
        r_ratios = trend_data.get("ratios", [])
        if r_days >= 3:
            trend_str = f"📈 連續上升 {r_days} 日（{' → '.join(str(x)+'%' for x in r_ratios[-3:])}）"
        elif r_days <= -3:
            trend_str = f"📉 連續下降 {abs(r_days)} 日（{' → '.join(str(x)+'%' for x in r_ratios[-3:])}）"
        else:
            trend_str = f"{'↗' if r_days>0 else '↘' if r_days<0 else '→'} 近期{'上升' if r_days>0 else '下降' if r_days<0 else '持平'}"

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
            f"持股趨勢：{trend_str}\n"
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

    elif text.startswith("/exdiv"):
        await send_message("⏳ 查詢除權息日曆中...", chat_id=chat_id)
        rows = await fetch_all(
            "SELECT stock_code FROM stocks WHERE is_active=TRUE"
        )
        codes   = [r["stock_code"] for r in rows]
        events  = await fetch_upcoming_exdiv(codes, days_ahead=14)
        if not events:
            await send_message("📅 未來 14 日內追蹤股無除權息事件", chat_id=chat_id)
        else:
            lines = ["📅 *追蹤股除權息日曆（未來 14 日）*\n"]
            for ev in events:
                cash  = f"現金 {ev['cash_dividend']} 元" if ev.get("cash_dividend") else ""
                stock = f"股票 {ev['stock_dividend']} 元" if ev.get("stock_dividend") else ""
                div_info = " / ".join(filter(None, [cash, stock])) or ev.get("dividend_type", "")
                lines.append(
                    f"⚠️ `{ev['stock_code']}` {ev['stock_name']}\n"
                    f"    除權息日：{ev['ex_date']}  最後買進：{ev['last_buy_date']}\n"
                    f"    {div_info}"
                )
            await send_message("\n".join(lines), chat_id=chat_id)

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


async def _chip_reversal_alert(trade_date: datetime.date):
    """
    籌碼轉向偵測：昨日外資連賣（consec < 0）→ 今日轉買（foreign_net > 0）。
    偵測到後推播 Telegram 做為短線進場參考。
    """
    # 找出今日轉買 + 昨日為連賣狀態的股票
    rows = await fetch_all("""
        SELECT t.stock_code, s.stock_name,
               t.foreign_net  AS today_net,
               y.foreign_consec AS yest_consec
        FROM institutional_daily t
        JOIN institutional_daily y
          ON t.stock_code = y.stock_code
         AND y.trade_date = (
               SELECT MAX(trade_date) FROM institutional_daily
               WHERE stock_code = t.stock_code AND trade_date < $1
             )
        JOIN stocks s ON s.stock_code = t.stock_code
        WHERE t.trade_date = $1
          AND t.foreign_net > 0
          AND y.foreign_consec <= -3
        ORDER BY y.foreign_consec ASC
    """, trade_date)

    if not rows:
        logger.info(f"[轉向] {trade_date} 無籌碼轉向訊號")
        return

    lines = [f"🔄 *籌碼轉向訊號*（{trade_date}）\n"]
    lines.append("以下股票外資由賣轉買，短線留意：\n")
    for r in rows:
        lines.append(
            f"✅ `{r['stock_code']}` {r['stock_name']}\n"
            f"    昨連賣 {abs(r['yest_consec'])} 日 → 今轉買 +{r['today_net']:,} 張"
        )
    await send_message("\n".join(lines))
    logger.info(f"[轉向] 推播籌碼轉向訊號：{len(rows)} 檔")


async def _foreign_sell_alert(trade_date: datetime.date, threshold: int = 5):
    """外資連賣預警：連賣天數 ≥ threshold 的股票推播到 Telegram。"""
    rows = await fetch_all("""
        SELECT d.stock_code, s.stock_name, d.foreign_consec, d.foreign_net
        FROM institutional_daily d
        JOIN stocks s ON s.stock_code = d.stock_code
        WHERE d.trade_date = $1
          AND d.foreign_consec <= -$2
        ORDER BY d.foreign_consec ASC
    """, trade_date, threshold)

    if not rows:
        logger.info(f"[預警] {trade_date} 無外資連賣 ≥{threshold} 日的股票")
        return

    lines = [f"⚠️ *外資連賣預警*（{trade_date}）\n"]
    lines.append(f"以下股票外資連賣 ≥ {threshold} 日，請注意籌碼風險：\n")
    for r in rows:
        net_str = f"{r['foreign_net']:,}" if r['foreign_net'] >= 0 else f"{r['foreign_net']:,}"
        lines.append(
            f"🔴 `{r['stock_code']}` {r['stock_name']} "
            f"連賣 *{abs(r['foreign_consec'])} 日*  今日：{net_str} 張"
        )
    await send_message("\n".join(lines))
    logger.info(f"[預警] 推播外資連賣預警：{len(rows)} 檔")


async def _scheduled_run():
    _tz_taipei = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(_tz_taipei).date()
    logger.info(f"[排程] 22:00 自動觸發篩選：{today}")
    # 並行執行篩選 + 法人資料更新
    await asyncio.gather(
        _screening_worker(today),
        _update_institutional_cache(today),
    )
    # 法人資料更新完成後，並行推播：外資連賣預警 + 籌碼轉向訊號
    await asyncio.gather(
        _foreign_sell_alert(today),
        _chip_reversal_alert(today),
    )
    # 除權息預警（14 日內）
    stocks = await fetch_all("SELECT stock_code FROM stocks WHERE is_active=TRUE")
    codes  = [r["stock_code"] for r in stocks]
    exdivs = await fetch_upcoming_exdiv(codes, days_ahead=14)
    if exdivs:
        lines = [f"📅 *除權息預警*（未來 14 日內）\n"]
        for ev in exdivs:
            cash = f"現金 {ev['cash_dividend']}元" if ev.get("cash_dividend") else ""
            lines.append(f"⚠️ `{ev['stock_code']}` {ev['stock_name']}  除權息日 {ev['ex_date']}  {cash}")
        await send_message("\n".join(lines))


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


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


@app.get("/api/sector-rotation")
async def sector_rotation(trade_date: Optional[str] = Query(None, description="YYYY-MM-DD，預設今天")):
    """板塊輪動即時資料，供 tw-sector-rotation-map.html 拉取。"""
    from utils.sector_rotation import build_sector_rotation
    try:
        data = await build_sector_rotation(today=trade_date)
        return JSONResponse(content=data)
    except Exception as e:
        logger.error(f"sector_rotation 失敗: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})



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


@app.post("/institutional/alert")
async def trigger_foreign_alert(
    trade_date: Optional[str] = Query(None),
    threshold: int = Query(5, description="連賣天數門檻，預設 5 日"),
):
    """手動觸發外資連賣預警推播。"""
    _tz = datetime.timezone(datetime.timedelta(hours=8))
    td  = (datetime.date.fromisoformat(trade_date)
           if trade_date else datetime.datetime.now(_tz).date())
    await _foreign_sell_alert(td, threshold)
    return {"status": "預警推播完成", "date": str(td), "threshold": threshold}


@app.get("/exdiv")
async def get_exdiv(days_ahead: int = Query(14, description="查詢未來幾日")):
    """查詢追蹤股未來除權息清單。"""
    stocks = await fetch_all("SELECT stock_code FROM stocks WHERE is_active=TRUE")
    codes  = [r["stock_code"] for r in stocks]
    events = await fetch_upcoming_exdiv(codes, days_ahead=days_ahead)
    return {"days_ahead": days_ahead, "count": len(events), "events": events}


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
    _tz = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(_tz).date()
    now_str = datetime.datetime.now(_tz).strftime("%Y-%m-%d %H:%M")

    # ── 資料並行抓取 ────────────────────────────────────────────────────────────
    results, logs, inst_rows, stocks_all = await asyncio.gather(
        fetch_all("""
            SELECT rank, stock_code, stock_name, sector,
                   ROUND((daily_return*100)::numeric,2)    AS ret_pct,
                   ROUND(volume_ratio::numeric,2)          AS vr,
                   ROUND((turnover_rate*100)::numeric,2)   AS tr_pct,
                   ROUND(float_mktcap_bn::numeric,1)       AS mktcap,
                   ROUND((relative_strength*100)::numeric,2) AS rs_pct,
                   ROUND(rsi_14::numeric,1) AS rsi,
                   ROUND(composite_score::numeric,3) AS score,
                   screen_date
            FROM screening_results
            WHERE screen_date = (SELECT MAX(screen_date) FROM screening_results)
            ORDER BY rank ASC
        """),
        fetch_all(
            "SELECT run_date, status, candidates, error_msg, started_at "
            "FROM workflow_logs ORDER BY started_at DESC LIMIT 8"
        ),
        fetch_all("""
            SELECT i.stock_code, s.stock_name,
                   i.foreign_net, i.trust_net, i.dealer_net, i.total_net,
                   i.foreign_consec, i.foreign_ratio, i.trade_date
            FROM institutional_daily i
            JOIN stocks s ON s.stock_code = i.stock_code
            WHERE i.trade_date = (SELECT MAX(trade_date) FROM institutional_daily)
              AND s.is_active = TRUE
            ORDER BY ABS(i.foreign_net) DESC
        """),
        fetch_all("SELECT stock_code FROM stocks WHERE is_active=TRUE"),
    )

    codes = [r["stock_code"] for r in stocks_all]
    exdiv_events = await fetch_upcoming_exdiv(codes, days_ahead=14)

    # 篩選結果：若無資料取最近執行紀錄的日期
    if results:
        date_str = str(results[0]["screen_date"])
    else:
        last_log = next((l for l in logs if l["status"] in ("SUCCESS","FAILED")), None)
        date_str = str(last_log["run_date"]) + "（0候選，市場動能不足）" if last_log else "尚無執行紀錄"
    inst_date = str(inst_rows[0]["trade_date"]) if inst_rows else "尚無資料"

    # ── 篩選結果表格 ─────────────────────────────────────────────────────────────
    def score_bar(score):
        pct = min(int(float(score) * 100), 100)
        return (f'<div style="display:flex;align-items:center;gap:6px">'
                f'<div style="flex:1;background:#1e293b;border-radius:4px;height:6px">'
                f'<div style="width:{pct}%;background:#38bdf8;height:6px;border-radius:4px"></div></div>'
                f'<span style="font-size:11px;color:#94a3b8;min-width:34px">{score}</span></div>')

    def consec_badge(c):
        if c is None: return '<span style="color:#64748b">—</span>'
        c = int(c)
        if c >= 3:   return f'<span style="color:#22c55e;font-weight:600">連買{c}日▲</span>'
        if c > 0:    return f'<span style="color:#86efac">連買{c}日↑</span>'
        if c <= -5:  return f'<span style="color:#ef4444;font-weight:600">連賣{abs(c)}日▼</span>'
        if c < 0:    return f'<span style="color:#fca5a5">連賣{abs(c)}日↓</span>'
        return '<span style="color:#64748b">持平</span>'

    def net_cell(v):
        v = int(v or 0)
        if v > 0:  return f'<span style="color:#22c55e">+{v:,}</span>'
        if v < 0:  return f'<span style="color:#ef4444">{v:,}</span>'
        return '<span style="color:#64748b">0</span>'

    screen_rows = ""
    for r in results:
        rsi_v = float(r["rsi"]) if r["rsi"] else 0
        rsi_c = "#ef4444" if rsi_v > 70 else "#f59e0b" if rsi_v > 60 else "#22c55e"
        ret   = float(r["ret_pct"] or 0)
        ret_c = "#22c55e" if ret > 0 else "#ef4444"
        screen_rows += f"""<tr>
          <td style="color:#64748b">{r['rank']}</td>
          <td><span style="font-weight:700;color:#38bdf8">{r['stock_code']}</span></td>
          <td>{r['stock_name']}</td>
          <td style="color:#64748b;font-size:11px">{r['sector'] or '—'}</td>
          <td style="color:{ret_c};font-weight:600">{'+' if ret>0 else ''}{ret:.2f}%</td>
          <td style="color:#f59e0b">{r['vr']}×</td>
          <td>{r['tr_pct'] or '—'}%</td>
          <td style="color:#94a3b8">{r['mktcap'] or '—'}億</td>
          <td style="color:#a78bfa">+{r['rs_pct']}%</td>
          <td style="color:{rsi_c}">{r['rsi'] or '—'}</td>
          <td>{score_bar(r['score'])}</td>
        </tr>"""

    # ── 三大法人表格 ─────────────────────────────────────────────────────────────
    inst_html = ""
    for r in inst_rows:
        ratio = float(r["foreign_ratio"] or 0)
        ratio_bar = (f'<div style="display:flex;align-items:center;gap:4px">'
                     f'<div style="width:60px;background:#1e293b;border-radius:3px;height:5px">'
                     f'<div style="width:{min(ratio,100):.0f}%;background:#818cf8;height:5px;border-radius:3px"></div></div>'
                     f'<span style="font-size:11px;color:#94a3b8">{ratio:.1f}%</span></div>')
        inst_html += f"""<tr>
          <td><span style="font-weight:600;color:#e2e8f0">{r['stock_code']}</span></td>
          <td style="color:#94a3b8">{r['stock_name']}</td>
          <td>{net_cell(r['foreign_net'])}</td>
          <td>{net_cell(r['trust_net'])}</td>
          <td>{net_cell(r['dealer_net'])}</td>
          <td>{net_cell(r['total_net'])}</td>
          <td>{consec_badge(r['foreign_consec'])}</td>
          <td>{ratio_bar}</td>
        </tr>"""

    # ── 除權息卡片 ─────────────────────────────────────────────────────────────
    exdiv_html = ""
    for ev in exdiv_events:
        cash = ev.get("cash_dividend", "")
        exdiv_html += f"""<div style="background:#1e293b;border-left:3px solid #f59e0b;padding:10px 14px;border-radius:6px;margin-bottom:8px">
          <span style="color:#f59e0b;font-weight:700">{ev['stock_code']}</span>
          <span style="color:#e2e8f0;margin:0 8px">{ev['stock_name']}</span>
          <span style="color:#64748b;font-size:12px">除權息日 {ev['ex_date']} ｜ 最後買進 {ev['last_buy_date']}</span>
          {'<span style="color:#fbbf24;font-size:12px;margin-left:8px">現金 '+cash+'元</span>' if cash else ''}
        </div>"""

    # ── 執行紀錄 ────────────────────────────────────────────────────────────────
    log_html = ""
    for l in logs:
        sc = "#22c55e" if l["status"] == "SUCCESS" else "#ef4444" if l["status"] == "FAILED" else "#f59e0b"
        badge_bg = "#166534" if l["status"] == "SUCCESS" else "#7f1d1d" if l["status"] == "FAILED" else "#78350f"
        err = (l["error_msg"] or "")[:60]
        log_html += f"""<tr>
          <td style="color:#94a3b8">{l['run_date']}</td>
          <td><span style="background:{badge_bg};color:{sc};padding:2px 8px;border-radius:999px;font-size:11px">{l['status']}</span></td>
          <td style="color:#e2e8f0">{l['candidates'] or '—'}</td>
          <td style="font-size:11px;color:#64748b">{err}</td>
        </tr>"""

    no_data = '<tr><td colspan="99" style="text-align:center;padding:30px;color:#64748b">尚無資料</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>台股動量篩選儀表板</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0a0f1e;color:#e2e8f0;font-family:-apple-system,'PingFang TC','Microsoft JhengHei',sans-serif;min-height:100vh}}
  .navbar{{background:#0f172a;border-bottom:1px solid #1e293b;padding:10px 20px;
    display:flex;align-items:center;gap:6px;flex-wrap:wrap;position:sticky;top:0;z-index:101}}
  .navbar span{{font-size:13px;color:#38bdf8;font-weight:700;margin-right:8px}}
  .navbar a{{font-size:12px;color:#64748b;text-decoration:none;padding:4px 10px;
    border-radius:4px;border:1px solid #1e293b}}
  .navbar a:hover,.navbar a.active{{color:#e2e8f0;background:#1e293b}}
  .topbar{{background:#0f172a;border-bottom:1px solid #1e293b;padding:12px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:41px;z-index:100}}
  .topbar h1{{font-size:17px;color:#38bdf8;font-weight:700;letter-spacing:.5px}}
  .topbar .meta{{font-size:12px;color:#475569;display:flex;gap:16px;align-items:center}}
  .refresh-dot{{width:8px;height:8px;border-radius:50%;background:#22c55e;animation:pulse 2s infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
  .main{{padding:20px 24px;max-width:1400px;margin:0 auto}}
  .section-title{{font-size:13px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:12px;display:flex;align-items:center;gap:8px}}
  .section-title::after{{content:'';flex:1;height:1px;background:#1e293b}}
  .card{{background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:16px 20px;margin-bottom:24px}}
  .stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}}
  .stat{{background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:14px 16px}}
  .stat .label{{font-size:11px;color:#64748b;margin-bottom:4px}}
  .stat .val{{font-size:22px;font-weight:700;color:#e2e8f0}}
  .stat .sub{{font-size:11px;color:#475569;margin-top:2px}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th{{background:#080d1a;color:#475569;padding:9px 10px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #1e293b;position:sticky;top:49px}}
  td{{padding:10px 10px;border-bottom:1px solid #0f172a}}
  tr:hover td{{background:#111827}}
  .tabs{{display:flex;gap:2px;margin-bottom:16px}}
  .tab{{padding:6px 16px;border-radius:6px;font-size:13px;cursor:pointer;color:#64748b;border:none;background:none}}
  .tab.active{{background:#1e293b;color:#e2e8f0;font-weight:600}}
  .tab-content{{display:none}}.tab-content.active{{display:block}}
  .countdown{{font-size:11px;color:#475569}}
  @media(max-width:768px){{.main{{padding:12px}}.stat-grid{{grid-template-columns:repeat(2,1fr)}}}}
</style>
</head>
<body>
<div class="navbar">
  <span>🐻 台股監控</span>
  <a href="https://twstock-agent-1781283629.zeabur.app/dashboard">📊 量化系統</a>
  <a href="https://momentum-screener.zeabur.app/dashboard" class="active">⚡ 動量篩選</a>
  <a href="https://ic-screener.zeabur.app/dashboard">🔬 委屈股</a>
  <a href="https://bear-signal-service.zeabur.app/dashboard">🐻 空頭信號</a>
  <a href="https://bear-signal-service.zeabur.app/stop-loss">🛑 停損預警</a>
</div>
<div class="topbar">
  <h1>⚡ 台股動量篩選儀表板</h1>
  <div class="meta">
    <div class="refresh-dot"></div>
    <span>排程 22:00</span>
    <span>更新：{now_str}</span>
    <span id="cd" class="countdown" style="color:#38bdf8"></span>
  </div>
</div>
<div class="main">

<!-- KPI 卡片 -->
<div class="stat-grid">
  <div class="stat">
    <div class="label">本日候選股</div>
    <div class="val" style="color:#38bdf8">{len(results)}</div>
    <div class="sub">篩選日期：{date_str}</div>
  </div>
  <div class="stat">
    <div class="label">追蹤股總數</div>
    <div class="val">{len(codes)}</div>
    <div class="sub">法人資料：{inst_date}</div>
  </div>
  <div class="stat">
    <div class="label">法人資料股數</div>
    <div class="val">{len(inst_rows)}</div>
    <div class="sub">全數追蹤股</div>
  </div>
  <div class="stat">
    <div class="label">除權息預警</div>
    <div class="val" style="color:{'#f59e0b' if exdiv_events else '#22c55e'}">{len(exdiv_events)}</div>
    <div class="sub">未來14日內</div>
  </div>
</div>

<!-- 主內容 Tab -->
<div class="tabs">
  <button class="tab active" onclick="switchTab('screen')">🎯 篩選結果</button>
  <button class="tab" onclick="switchTab('inst')">🏦 三大法人</button>
  <button class="tab" onclick="switchTab('exdiv')">📅 除權息</button>
  <button class="tab" onclick="switchTab('logs')">📋 執行紀錄</button>
</div>

<div id="screen" class="tab-content active">
  <div class="card">
    <div class="section-title">動量候選股（{date_str}）</div>
    <div style="overflow-x:auto">
    <table>
      <thead><tr>
        <th>#</th><th>代碼</th><th>名稱</th><th>類股</th>
        <th>漲幅</th><th>量比</th><th>換手率</th><th>市值</th>
        <th>相對強度</th><th>RSI</th><th>綜合分</th>
      </tr></thead>
      <tbody>{screen_rows or no_data}</tbody>
    </table>
    </div>
  </div>
</div>

<div id="inst" class="tab-content">
  <div class="card">
    <div class="section-title">三大法人買賣超（{inst_date}，按外資絕對值排序）</div>
    <div style="overflow-x:auto">
    <table>
      <thead><tr>
        <th>代碼</th><th>名稱</th>
        <th>外資(張)</th><th>投信(張)</th><th>自營(張)</th><th>合計(張)</th>
        <th>外資連續</th><th>外資持股</th>
      </tr></thead>
      <tbody>{inst_html or no_data}</tbody>
    </table>
    </div>
  </div>
</div>

<div id="exdiv" class="tab-content">
  <div class="card">
    <div class="section-title">除權息預警（追蹤股，未來14日）</div>
    {exdiv_html or '<p style="color:#64748b;padding:20px 0">未來14日內無除權息事件</p>'}
  </div>
</div>

<div id="logs" class="tab-content">
  <div class="card">
    <div class="section-title">排程執行紀錄</div>
    <table>
      <thead><tr><th>日期</th><th>狀態</th><th>候選數</th><th>錯誤訊息</th></tr></thead>
      <tbody>{log_html or no_data}</tbody>
    </table>
  </div>
</div>

</div>
<script>
function switchTab(id){{
  document.querySelectorAll('.tab-content').forEach(el=>el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el=>el.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  event.target.classList.add('active');
}}
let s=300;const cd=document.getElementById('cd');
setInterval(()=>{{s--;cd.textContent=s>0?`${{Math.floor(s/60)}}:${{String(s%60).padStart(2,'0')}} 後刷新`:'刷新中...';if(s<=0)location.reload();}},1000);
</script>
</body></html>"""

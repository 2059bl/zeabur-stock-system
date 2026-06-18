"""
Bear Signal Service v1.1
========================
外資離場 9 維空頭信號 + 產業輪動警示 + 新聞情緒掃描 + 停損預警

排程：
  每日 14:30 Asia/Taipei — 停損預警（盤中最後半小時）
  每日 22:30 Asia/Taipei — 主信號計算（stock-ai-agent 22:00 跑完後）

共用 DB：與 stock-ai-agent / ic-screener 同一個 PostgreSQL
新增表：bear_market_indicators（不修改現有表）
"""
import asyncio
import logging
import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from utils.db       import get_pool, fetch_all, execute
from utils.notifier import send
from agents.bear_signal import compute_bear_signal, save_signal
from agents.stop_loss   import check_stop_loss, notify_stop_loss

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger    = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Asia/Taipei")
_TZ8      = datetime.timezone(datetime.timedelta(hours=8))

VERSION = "1.2.0"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bear_market_indicators (
    id               BIGSERIAL PRIMARY KEY,
    signal_date      DATE NOT NULL UNIQUE,
    total_score      NUMERIC(5,1),
    signal_level     VARCHAR(10),
    d1_foreign       NUMERIC(5,1),
    d2_trust         NUMERIC(5,1),
    d3_futures       NUMERIC(5,1),
    d4_currency      NUMERIC(5,1),
    d5_margin        NUMERIC(5,1),
    d6_short         NUMERIC(5,1),
    d7_index         NUMERIC(5,1),
    d8_rotation      NUMERIC(5,1),
    d9_news          NUMERIC(5,1),
    foreign_sell_days   INT,
    futures_net_short   BIGINT,
    usdtwd_rate         NUMERIC(7,4),
    usdtwd_deprec_pct   NUMERIC(6,3),
    margin_chg_pct      NUMERIC(6,2),
    short_ratio         NUMERIC(6,2),
    index_m1_pct        NUMERIC(6,2),
    weakening_pools     TEXT,
    news_summary        TEXT,
    news_risk_level     VARCHAR(10),
    news_black_swan     TEXT,
    action_text         TEXT,
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE bear_market_indicators ADD COLUMN IF NOT EXISTS d9_news NUMERIC(5,1);
ALTER TABLE bear_market_indicators ADD COLUMN IF NOT EXISTS news_summary TEXT;
ALTER TABLE bear_market_indicators ADD COLUMN IF NOT EXISTS news_risk_level VARCHAR(10);
ALTER TABLE bear_market_indicators ADD COLUMN IF NOT EXISTS news_black_swan TEXT;
"""


async def _run_daily_signal():
    """每日 22:30 執行：計算 9 維信號（含新聞）+ 推播。"""
    now = datetime.datetime.now(_TZ8)
    logger.info(f"[Bear Signal] 開始計算 {now.date()}")
    try:
        result = await compute_bear_signal()
        await save_signal(result)
        await _send_report(result)
        logger.info(f"[Bear Signal] 完成 score={result['score']} level={result['level']}")
    except Exception as e:
        logger.exception(f"[Bear Signal] 執行失敗: {e}")


async def _run_stop_loss_check():
    """每日 14:30 執行：停損預警掃描。"""
    now = datetime.datetime.now(_TZ8)
    # 週末跳過
    if now.weekday() >= 5:
        return
    logger.info(f"[StopLoss] 開始掃描 {now.date()}")
    try:
        result = await check_stop_loss()
        if result["triggered"]:
            await notify_stop_loss(result)
        logger.info(f"[StopLoss] 完成，觸發 {len(result['triggered'])} 個預警")
    except Exception as e:
        logger.exception(f"[StopLoss] 執行失敗: {e}")


async def _send_report(r: dict):
    """格式化 Telegram 推播。"""
    level_emoji = {"EXTREME": "🔴", "DANGER": "🟠", "WARNING": "🟡", "NORMAL": "🟢"}
    emoji = level_emoji.get(r["level"], "⚪")

    lines = [
        f"{emoji} *外資離場空頭信號 {r['date']}*",
        f"綜合評分：*{r['score']}/100*  等級：*{r['level']}*",
        f"建議：{r['action']}",
        "",
        "*9 維信號分解：*",
    ]
    for dim, score in r["scores"].items():
        bar = "█" * int(score // 10) + "░" * (10 - int(score // 10))
        lines.append(f"`{dim:<14}` {bar} {score:.0f}")

    lines += [
        "",
        "*關鍵數據：*",
        f"• 台指期外資淨空單：{r['futures_net_short']:,} 口",
        f"• USD/TWD：{r['usdtwd_rate']} （月變化 {r['usdtwd_deprec_pct']:+.2f}%）",
        f"• 大盤月漲幅：{r['index_m1_pct']:+.1f}%" if r['index_m1_pct'] else "• 大盤：無資料",
        f"• 外資現貨連賣：{r['foreign_sell_days']} 日",
    ]

    if r.get("weakening_pools"):
        lines += ["", "⚠️ *產業輪動警示（轉弱池）：*"]
        for pool in r["weakening_pools"]:
            lines.append(f"  • {pool}")

    # 新聞情緒摘要
    news_risk = r.get("news_risk_level", "LOW")
    news_summary = r.get("news_summary", "")
    if news_risk in ("HIGH", "EXTREME") or r.get("news_black_swan"):
        lines += ["", f"📰 *國際新聞風險：{news_risk}*"]
        if news_summary:
            lines.append(f"  {news_summary}")
        for hit in r.get("news_black_swan", [])[:3]:
            lines.append(f"  🚨 {hit}")

    await send("\n".join(lines))


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
    logger.info("DB Schema 初始化完成")

    scheduler.add_job(
        _run_daily_signal,
        CronTrigger(hour=22, minute=30, timezone="Asia/Taipei"),
        id="daily_bear_signal", replace_existing=True,
    )
    scheduler.add_job(
        _run_stop_loss_check,
        CronTrigger(hour=14, minute=30, timezone="Asia/Taipei"),
        id="daily_stop_loss", replace_existing=True,
    )
    scheduler.start()
    logger.info("排程啟動：22:30 主信號 / 14:30 停損預警")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Bear Signal Service", version=VERSION, lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status":  "ok",
        "version": VERSION,
        "time":    datetime.datetime.now(_TZ8).isoformat(),
    }


@app.post("/run/signal")
async def manual_signal():
    """手動觸發信號計算（背景執行）。"""
    asyncio.create_task(_run_daily_signal())
    return {"status": "triggered", "date": str(datetime.date.today())}


@app.post("/run/stop-loss")
async def manual_stop_loss():
    """手動觸發停損預警掃描。"""
    asyncio.create_task(_run_stop_loss_check())
    return {"status": "triggered", "date": str(datetime.date.today())}


@app.get("/alerts/stop-loss")
async def stop_loss_check():
    """即時查詢停損預警（不推播）。"""
    result = await check_stop_loss()
    return result


@app.get("/news/latest")
async def latest_news(force: bool = False):
    """
    查詢新聞情緒（快取 1 小時）。
    ?force=true 可強制重新抓取並呼叫 LLM。
    """
    from utils.news_scanner import scan_news, _news_cache
    import time
    result = await scan_news(force=force)
    cache_age = int(time.monotonic() - _news_cache["ts"]) if _news_cache["ts"] else 0
    return {**result, "cache_age_seconds": cache_age}


@app.get("/signal/latest")
async def latest_signal():
    """取得最新信號。"""
    rows = await fetch_all("""
        SELECT * FROM bear_market_indicators
        ORDER BY signal_date DESC LIMIT 1
    """)
    return rows[0] if rows else {}


@app.get("/signal/history")
async def signal_history(limit: int = 30):
    return await fetch_all("""
        SELECT signal_date, total_score, signal_level,
               futures_net_short, usdtwd_rate, usdtwd_deprec_pct,
               index_m1_pct, weakening_pools, action_text
        FROM bear_market_indicators
        ORDER BY signal_date DESC LIMIT $1
    """, limit)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    now_str = datetime.datetime.now(_TZ8).strftime("%Y-%m-%d %H:%M")
    rows = await fetch_all("""
        SELECT * FROM bear_market_indicators ORDER BY signal_date DESC LIMIT 30
    """)
    latest = rows[0] if rows else {}

    level = latest.get("signal_level", "N/A")
    score = latest.get("total_score", 0)
    level_color = {"EXTREME": "#ef4444", "DANGER": "#f97316",
                   "WARNING": "#eab308", "NORMAL": "#22c55e"}.get(level, "#64748b")

    dims = [
        ("D1 外資現貨", "d1_foreign"),
        ("D2 投信現貨", "d2_trust"),
        ("D3 台指期空單", "d3_futures"),
        ("D4 台幣貶值", "d4_currency"),
        ("D5 融資減少", "d5_margin"),
        ("D6 融券增加", "d6_short"),
        ("D7 大盤走勢", "d7_index"),
        ("D8 產業輪動", "d8_rotation"),
        ("D9 新聞情緒", "d9_news"),
    ]
    dims_html = ""
    for label, key in dims:
        v = float(latest.get(key) or 0)
        bar_w = int(v)
        bar_c = "#ef4444" if v >= 75 else "#f97316" if v >= 55 else "#eab308" if v >= 35 else "#22c55e"
        dims_html += f"""
        <div style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">
            <span style="color:#94a3b8">{label}</span>
            <span style="color:#e2e8f0;font-weight:600">{v:.0f}</span>
          </div>
          <div style="background:#1e293b;border-radius:4px;height:8px">
            <div style="width:{bar_w}%;background:{bar_c};height:8px;border-radius:4px;transition:width .3s"></div>
          </div>
        </div>"""

    history_html = ""
    for r in rows:
        lc = {"EXTREME": "#ef4444", "DANGER": "#f97316",
              "WARNING": "#eab308", "NORMAL": "#22c55e"}.get(r.get("signal_level"), "#64748b")
        wp = r.get("weakening_pools") or "—"
        idx_pct_str = (f"{r.get('index_m1_pct'):+.1f}%" if r.get('index_m1_pct') else '—')
        nr = r.get("news_risk_level") or "LOW"
        nrc = {"EXTREME":"#ef4444","HIGH":"#f97316","MEDIUM":"#eab308","LOW":"#22c55e"}.get(nr,"#64748b")
        history_html += f"""<tr>
          <td style="color:#94a3b8">{r['signal_date']}</td>
          <td style="color:{lc};font-weight:700">{r.get('total_score',0)}</td>
          <td><span style="color:{lc}">{r.get('signal_level','—')}</span></td>
          <td style="color:#94a3b8">{r.get('futures_net_short') or '—'}</td>
          <td style="color:#94a3b8">{r.get('usdtwd_rate') or '—'}</td>
          <td style="color:#94a3b8">{idx_pct_str}</td>
          <td style="font-size:11px;color:#64748b">{(wp[:40] + '…') if len(wp) > 40 else wp}</td>
          <td style="color:{nrc};font-size:11px;font-weight:600">{nr}</td>
        </tr>"""

    wp_list = latest.get("weakening_pools") or ""
    wp_html = "".join(f'<div style="color:#f97316;font-size:13px">⚠ {p}</div>'
                      for p in wp_list.split(",") if p.strip()) if wp_list else \
              '<div style="color:#22c55e;font-size:13px">✅ 無轉弱產業池</div>'

    news_risk = latest.get("news_risk_level") or "LOW"
    news_color = {"EXTREME": "#ef4444", "HIGH": "#f97316",
                  "MEDIUM": "#eab308", "LOW": "#22c55e"}.get(news_risk, "#64748b")
    news_summary = latest.get("news_summary") or "—"
    black_swan_raw = latest.get("news_black_swan") or ""
    black_swan_html = "".join(
        f'<div style="color:#ef4444;font-size:12px;margin-top:4px">🚨 {h}</div>'
        for h in black_swan_raw.split(";") if h.strip()
    ) if black_swan_raw else ""

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>外資離場信號儀表板</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;600;700&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0f1e;color:#e2e8f0;font-family:'Noto Sans TC',sans-serif}}
.topbar{{background:#0f172a;border-bottom:1px solid #1e293b;padding:12px 24px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}}
.topbar h1{{font-size:17px;color:#38bdf8;font-weight:700}}
.meta{{font-size:12px;color:#475569;display:flex;gap:16px;align-items:center}}
.dot{{width:8px;height:8px;border-radius:50%;background:#22c55e;animation:pulse 2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
.main{{padding:20px 24px;max-width:1400px;margin:0 auto}}
.grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:20px}}
.card{{background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:16px 20px}}
.card-title{{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:12px}}
.score-big{{font-size:56px;font-weight:700;color:{level_color};line-height:1}}
.level-badge{{display:inline-block;padding:4px 12px;border-radius:999px;font-size:12px;
  font-weight:600;background:{level_color}22;color:{level_color};margin-top:8px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:#080d1a;color:#475569;padding:9px 8px;text-align:left;
  font-size:11px;border-bottom:1px solid #1e293b;white-space:nowrap}}
td{{padding:9px 8px;border-bottom:1px solid #0a0f1e;white-space:nowrap}}
tr:hover td{{background:#111827}}
</style>
</head>
<body>
<div class="topbar">
  <h1>🐻 外資離場空頭信號系統</h1>
  <div class="meta">
    <div class="dot"></div>
    <span>5分鐘自動刷新</span>
    <span>更新：{now_str}</span>
  </div>
</div>
<div class="main">

<div class="grid">
  <div class="card">
    <div class="card-title">綜合空頭信號</div>
    <div class="score-big">{score}</div>
    <div class="level-badge">{level}</div>
    <div style="font-size:12px;color:#94a3b8;margin-top:10px">{latest.get('action_text','—')}</div>
  </div>
  <div class="card">
    <div class="card-title">8 維信號分解</div>
    {dims_html}
  </div>
  <div class="card">
    <div class="card-title">關鍵數據</div>
    <table>
      <tr><td style="color:#64748b;font-size:12px">台指期外資淨空單</td>
          <td style="color:#f87171;font-weight:600">{int(latest.get('futures_net_short') or 0):,} 口</td></tr>
      <tr><td style="color:#64748b;font-size:12px">USD/TWD</td>
          <td style="color:#e2e8f0">{latest.get('usdtwd_rate') or '—'}</td></tr>
      <tr><td style="color:#64748b;font-size:12px">台幣月貶值</td>
          <td style="color:#f87171">{(str(f"{latest.get('usdtwd_deprec_pct'):+.2f}") + "%") if latest.get('usdtwd_deprec_pct') else '—'}</td></tr>
      <tr><td style="color:#64748b;font-size:12px">大盤月漲幅</td>
          <td style="color:#e2e8f0">{(str(f"{latest.get('index_m1_pct'):+.1f}") + "%") if latest.get('index_m1_pct') else '—'}</td></tr>
      <tr><td style="color:#64748b;font-size:12px">外資現貨連賣</td>
          <td style="color:#e2e8f0">{latest.get('foreign_sell_days') or 0} 日</td></tr>
    </table>
    <div class="card-title" style="margin-top:16px">產業輪動警示</div>
    {wp_html}
    <div class="card-title" style="margin-top:16px">
      📰 國際新聞風險
      <span style="color:{news_color};font-size:11px;margin-left:8px;font-weight:700">{news_risk}</span>
    </div>
    <div style="font-size:12px;color:#94a3b8;margin-bottom:4px">{news_summary}</div>
    {black_swan_html}
    <div style="margin-top:12px">
      <a href="/alerts/stop-loss" style="color:#38bdf8;font-size:12px;text-decoration:none">
        🛑 查看停損預警 →
      </a>
    </div>
  </div>
</div>

<div class="card">
  <div class="card-title">歷史信號趨勢</div>
  <div style="overflow-x:auto">
  <table>
    <thead><tr>
      <th>日期</th><th>評分</th><th>等級</th>
      <th>期貨淨空</th><th>USD/TWD</th><th>大盤月漲</th><th>轉弱產業</th><th>新聞風險</th>
    </tr></thead>
    <tbody>{history_html or '<tr><td colspan="8" style="text-align:center;padding:20px;color:#475569">尚未執行</td></tr>'}</tbody>
  </table>
  </div>
</div>

</div>
</body></html>"""

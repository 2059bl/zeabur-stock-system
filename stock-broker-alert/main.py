"""
stock-broker-alert — 主入口
FastAPI + APScheduler
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, HTMLResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from src.db import get_pool
    from src.universe import sync_universe
    from src.scheduler import start_scheduler
    from src.telegram import test_telegram

    logger.info("=== stock-broker-alert 啟動 ===")
    await get_pool()
    await sync_universe()

    scheduler = start_scheduler()
    app.state.scheduler = scheduler

    test_telegram()
    logger.info("系統就緒")
    yield

    scheduler.shutdown()
    from src.db import close_pool
    await close_pool()
    logger.info("系統關閉")


app = FastAPI(
    title="主力帶血承接警報系統",
    description="暴跌日帶血籌碼承接分析 ＋ 關鍵券商分點追蹤警報",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health", summary="服務健康狀態")
async def health():
    from src.finmind_client import get_today_count
    return {
        "status":     "ok",
        "service":    "stock-broker-alert",
        "date":       str(date.today()),
        "api_usage":  get_today_count(),
    }


# ── 手動觸發分析（歷史日期 or 今日）──────────────────────────────────────────────
@app.post("/analyze/blood-day", summary="觸發帶血日分析")
async def trigger_blood_analysis(
    trade_date: str = Query(..., description="分析日期 YYYY-MM-DD"),
    drop_threshold: float = Query(-4.0, description="大盤跌幅門檻（%，預設 -4.0）"),
    max_stocks: int = Query(60, description="最多分析股票數"),
):
    """手動觸發暴跌日帶血籌碼承接分析。"""
    from src.blood_absorption import analyze_blood_day
    from src.market_data import fetch_prices_batch
    from src.universe import get_all_codes

    # 先確保行情資料在 DB
    all_stocks = await get_all_codes()
    codes = [s["code"] for s in all_stocks]
    await fetch_prices_batch(codes, trade_date)

    results = await analyze_blood_day(
        trade_date, drop_threshold=drop_threshold,
        max_stocks=max_stocks,
    )
    blood = [r for r in results if r.get("is_blood_absorption")]
    return {
        "trade_date": trade_date,
        "total":      len(results),
        "blood":      len(blood),
        "results":    results,
    }


@app.get("/blood-day/report", summary="帶血承接分析報告")
async def get_blood_report(
    trade_date: str = Query(..., description="交易日 YYYY-MM-DD"),
    signal: str = Query("", description="信號篩選：STRONG_BUY / WATCH / AVOID / 空=全部"),
):
    """查詢指定日期的帶血承接分析結果。"""
    from src.db import fetch_all
    cond = "AND signal_tag=$2" if signal else ""
    args = [date.fromisoformat(trade_date)] + ([signal] if signal else [])
    rows = await fetch_all(
        f"""SELECT * FROM blood_absorption_report
            WHERE trade_date=$1 {cond}
            ORDER BY absorption_score DESC""",
        *args,
    )
    return {"trade_date": trade_date, "count": len(rows), "data": rows}


@app.get("/blood-day/report/markdown", summary="帶血承接報告（Markdown 格式）", response_class=PlainTextResponse)
async def get_blood_report_md(trade_date: str = Query(..., description="交易日 YYYY-MM-DD")):
    """暴跌日籌碼轉移總表（Markdown 純文字，適合複製到通訊軟體）。"""
    from src.db import fetch_all
    rows = await fetch_all(
        """SELECT trade_date, stock_code, stock_name, sector,
                  change_pct, volume, volume_ratio,
                  margin_change, financing_absorbed,
                  foreign_net, trust_net, dealer_net, total_inst_net,
                  gov_bank_net, pub_bank_status,
                  top_broker_1_code, top_broker_1_name, top_broker_1_net,
                  top_broker_2_code, top_broker_2_name, top_broker_2_net,
                  top_broker_3_code, top_broker_3_name, top_broker_3_net,
                  absorption_ratio, absorption_score, principal_type, signal_tag
           FROM blood_absorption_report
           WHERE trade_date=$1 ORDER BY absorption_score DESC""",
        date.fromisoformat(trade_date),
    )
    if not rows:
        return f"# 無資料：{trade_date}"

    lines = [
        f"# 暴跌日籌碼轉移總表 — {trade_date}",
        "",
        "| 日期 | 代號 | 名稱 | 族群 | 跌幅% | 成交量 | 融資洗出 | 外資 | 投信 | 自營 | 公股 | "
        "主承1 | 主承2 | 主承3 | 吞噬率% | 評分 | 主力性質 | 信號 |",
        "|------|------|------|------|-------|--------|---------|------|------|------|------|"
        "------|------|------|---------|------|---------|------|",
    ]
    for r in rows:
        def _v(x, d=0): return f"{x:.{d}f}" if x is not None else "—"
        b1 = f"{r['top_broker_1_name'] or ''}({_v(r['top_broker_1_net'])})" if r.get("top_broker_1_code") else "—"
        b2 = f"{r['top_broker_2_name'] or ''}({_v(r['top_broker_2_net'])})" if r.get("top_broker_2_code") else "—"
        b3 = f"{r['top_broker_3_name'] or ''}({_v(r['top_broker_3_net'])})" if r.get("top_broker_3_code") else "—"
        fin = "洗出" if (r.get("margin_change") or 0) < 0 else "增加"
        lines.append(
            f"| {r['trade_date']} | {r['stock_code']} | {r['stock_name'] or ''} | {r['sector'] or ''} | "
            f"{_v(r['change_pct'],1)}% | {r['volume'] or '—'} | "
            f"{fin}{abs(r['margin_change'] or 0)} | "
            f"{_v(r['foreign_net'])} | {_v(r['trust_net'])} | {_v(r['dealer_net'])} | "
            f"{_v(r['gov_bank_net'])} | "
            f"{b1} | {b2} | {b3} | "
            f"{_v(r['absorption_ratio'],1)}% | {_v(r['absorption_score'],1)} | "
            f"{r['principal_type'] or '無法判定'} | {r['signal_tag'] or '—'} |"
        )
    return "\n".join(lines)


# ── Broker Watchlist ──────────────────────────────────────────────────────────
@app.get("/watchlist", summary="主力券商觀察名單")
async def get_watchlist(active_only: bool = Query(True, description="True=僅顯示啟用中的券商")):
    from src.db import fetch_all
    cond = "WHERE active=TRUE" if active_only else ""
    rows = await fetch_all(
        f"SELECT * FROM broker_watchlist {cond} ORDER BY broker_score DESC"
    )
    return {"count": len(rows), "data": rows}


@app.post("/watchlist/rebuild", summary="重建主力券商觀察名單")
async def rebuild_watchlist():
    """從歷史分析結果重建 Watchlist。"""
    from src.broker_score import rebuild_watchlist_from_history
    await rebuild_watchlist_from_history()
    from src.db import fetch_all
    rows = await fetch_all("SELECT COUNT(*) AS cnt FROM broker_watchlist WHERE active=TRUE")
    return {"status": "ok", "watchlist_count": rows[0]["cnt"] if rows else 0}


# ── EPS 分析 ──────────────────────────────────────────────────────────────────
@app.get("/eps-analysis")
async def eps_analysis(
    codes: str = Query("", description="逗號分隔代碼；空=讀取三率三昇成長股"),
    fmt:   str = Query("json", description="json 或 md"),
):
    """三率三升個股 EPS 推估分析。"""
    from src.eps_analysis import run_eps_analysis, to_markdown_table
    from src.universe import get_all_codes

    if codes.strip():
        stock_list = [{"code": c.strip(), "name": c.strip()} for c in codes.split(",") if c.strip()]
    else:
        all_s = await get_all_codes()
        stock_list = all_s[:30]  # 預設前30支，節省 API

    results = await run_eps_analysis(stock_list)

    if fmt == "md":
        table = to_markdown_table(results)
        return PlainTextResponse(table)
    return {"count": len(results), "data": results}


# ── Broker Top 20 排行榜 ──────────────────────────────────────────────────────
@app.get("/broker/top20", summary="主力券商評分排行 Top 20")
async def broker_top20():
    from src.db import fetch_all
    rows = await fetch_all(
        """
        SELECT bw.broker_code, bw.broker_name, bw.broker_score,
               bw.blood_selling_count, bw.total_net_buy,
               bw.max_absorption_ratio, bw.day_trade_risk,
               bw.detected_sectors, bw.blood_selling_dates,
               COUNT(DISTINCT bda.stock_code) AS stock_count
        FROM broker_watchlist bw
        LEFT JOIN broker_daily_actions bda ON bw.broker_code=bda.broker_code AND bda.is_blood_day
        WHERE bw.active=TRUE
        GROUP BY bw.broker_code, bw.broker_name, bw.broker_score,
                 bw.blood_selling_count, bw.total_net_buy,
                 bw.max_absorption_ratio, bw.day_trade_risk,
                 bw.detected_sectors, bw.blood_selling_dates
        ORDER BY bw.broker_score DESC
        LIMIT 20
        """
    )
    return {"count": len(rows), "data": rows}


# ── API Quota 查詢 ────────────────────────────────────────────────────────────
@app.get("/api-quota", summary="FinMind API 用量查詢")
async def api_quota():
    from src.finmind_client import get_today_count
    from src.db import fetch_all
    db_log = await fetch_all("SELECT * FROM v_api_quota_today")
    return {
        "proc_count":  get_today_count(),
        "daily_limit": 8000,
        "remaining":   8000 - get_today_count(),
        "db_log":      db_log,
    }


# ── 警報記錄 ──────────────────────────────────────────────────────────────────
@app.get("/alerts", summary="券商警報記錄")
async def get_alerts(
    days: int = Query(7, description="查詢最近幾天（預設 7 天）"),
    broker_code: str = Query("", description="券商代碼篩選（空=全部）"),
):
    from src.db import fetch_all
    from datetime import timedelta
    start = str(date.today() - timedelta(days=days))
    cond  = "AND broker_code=$2" if broker_code else ""
    args  = [date.fromisoformat(start)] + ([broker_code] if broker_code else [])
    rows  = await fetch_all(
        f"SELECT * FROM broker_alerts WHERE alert_date >= $1 {cond} ORDER BY alert_date DESC",
        *args,
    )
    return {"count": len(rows), "data": rows}


# ── 手動執行今日警報掃描 ───────────────────────────────────────────────────────
@app.post("/alerts/run", summary="立即執行今日警報掃描")
async def run_alerts_now():
    from src.alert_engine import run_daily_alerts
    today = str(date.today())
    n = await run_daily_alerts(today)
    return {"status": "ok", "date": today, "alerts": n}


@app.get("/dashboard", summary="主力儀表板（HTML）", response_class=HTMLResponse)
async def dashboard():
    """統一 HTML 儀表板：Broker Top20 + 帶血報告查詢"""
    html = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>主力帶血承接儀表板</title>
<style>
:root{--bg:#0e1117;--panel:#161b27;--card:#1c2236;--border:#2a3148;--ink:#e2e8f0;--muted:#64748b;--ok:#10b981;--warn:#f59e0b;--danger:#ef4444;--blue:#3b82f6}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font-family:ui-sans-serif,system-ui,sans-serif;padding:20px}
h1{font-size:18px;font-weight:700;margin-bottom:4px}
.sub{font-size:12px;color:var(--muted);margin-bottom:20px}
.tabs{display:flex;gap:8px;margin-bottom:16px}
.tab{padding:8px 16px;border-radius:8px;background:var(--card);border:1px solid var(--border);cursor:pointer;font-size:13px;color:var(--muted)}
.tab.active{background:var(--blue);color:#fff;border-color:var(--blue)}
.pane{display:none}.pane.active{display:block}
/* controls */
.ctrl{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.ctrl input,.ctrl select{background:var(--card);border:1px solid var(--border);color:var(--ink);border-radius:6px;padding:6px 10px;font-size:13px}
.btn{padding:7px 14px;border-radius:6px;background:var(--blue);color:#fff;border:none;cursor:pointer;font-size:13px;font-weight:600}
.btn:hover{opacity:.85}
.btn-g{background:var(--ok)}
/* table */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th{background:var(--panel);color:var(--muted);text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap;position:sticky;top:0}
td{padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:top;white-space:nowrap}
tr:hover td{background:var(--panel)}
.pill{display:inline-block;padding:2px 7px;border-radius:10px;font-size:10px;font-weight:700}
.s90{background:color-mix(in srgb,var(--ok) 20%,transparent);color:var(--ok)}
.s70{background:color-mix(in srgb,var(--warn) 20%,transparent);color:var(--warn)}
.s50{background:color-mix(in srgb,var(--muted) 20%,transparent);color:var(--muted)}
.tag-buy{background:color-mix(in srgb,var(--ok) 20%,transparent);color:var(--ok)}
.tag-watch{background:color-mix(in srgb,var(--warn) 20%,transparent);color:var(--warn)}
.tag-avoid{background:color-mix(in srgb,var(--danger) 20%,transparent);color:var(--danger)}
.neg{color:var(--danger)}.pos{color:var(--ok)}
.note{color:var(--muted);font-size:12px;padding:12px 0}
</style>
</head>
<body>
<h1>🎯 主力帶血承接警報系統</h1>
<div class="sub">stock-broker-alert.zeabur.app</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('top20')">📊 主力排行 Top20</div>
  <div class="tab" onclick="switchTab('report')">🩸 帶血報告</div>
</div>

<!-- Tab: Top20 -->
<div class="pane active" id="pane-top20">
  <div class="tbl-wrap"><table id="t20">
    <thead><tr>
      <th>#</th><th>券商代碼</th><th>券商名稱</th><th>評分</th>
      <th>帶血次數</th><th>累計淨買(張)</th><th>最大吞噬率%</th>
      <th>當沖風險</th><th>持股數</th>
    </tr></thead>
    <tbody id="t20-body"><tr><td colspan="9" class="note">載入中…</td></tr></tbody>
  </table></div>
</div>

<!-- Tab: 帶血報告 -->
<div class="pane" id="pane-report">
  <div class="ctrl">
    <input type="date" id="rpt-date" value="">
    <select id="rpt-signal">
      <option value="">全部信號</option>
      <option value="STRONG_BUY">STRONG_BUY</option>
      <option value="WATCH">WATCH</option>
      <option value="AVOID">AVOID</option>
    </select>
    <button class="btn" onclick="loadReport()">查詢</button>
  </div>
  <div class="tbl-wrap"><table id="trpt">
    <thead><tr>
      <th>代號</th><th>名稱</th><th>族群</th><th>跌幅</th>
      <th>成交量</th><th>融資</th><th>外資</th><th>投信</th><th>自營</th><th>公股</th>
      <th>主承1</th><th>主承2</th><th>吞噬率%</th><th>評分</th><th>主力性質</th><th>信號</th>
    </tr></thead>
    <tbody id="trpt-body"><tr><td colspan="16" class="note">選擇日期後點查詢</td></tr></tbody>
  </table></div>
</div>

<script>
function switchTab(id){
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',['top20','report'][i]===id));
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('active'));
  document.getElementById('pane-'+id).classList.add('active');
}

// default date: yesterday
const d=new Date(); d.setDate(d.getDate()-1);
document.getElementById('rpt-date').value=d.toISOString().slice(0,10);

function scoreClass(s){return s>=90?'s90':s>=70?'s70':'s50'}
function tagClass(t){return t==='STRONG_BUY'?'tag-buy':t==='WATCH'?'tag-watch':'tag-avoid'}
function fmt(v,d=0){return v==null?'—':(+v).toLocaleString('zh-Hant',{maximumFractionDigits:d})}

async function loadTop20(){
  const r=await fetch('/broker/top20');
  const j=await r.json();
  const tb=document.getElementById('t20-body');
  if(!j.data||!j.data.length){tb.innerHTML='<tr><td colspan="9" class="note">無資料</td></tr>';return}
  tb.innerHTML=j.data.map((b,i)=>`<tr>
    <td>${i+1}</td>
    <td>${b.broker_code}</td>
    <td>${b.broker_name||'—'}</td>
    <td><span class="pill ${scoreClass(b.broker_score)}">${b.broker_score}</span></td>
    <td>${b.blood_selling_count??'—'}</td>
    <td class="${(b.total_net_buy||0)>=0?'pos':'neg'}">${fmt(b.total_net_buy)}</td>
    <td>${fmt(b.max_absorption_ratio,1)}</td>
    <td>${b.day_trade_risk||'—'}</td>
    <td>${b.stock_count??'—'}</td>
  </tr>`).join('');
}

async function loadReport(){
  const dt=document.getElementById('rpt-date').value;
  const sig=document.getElementById('rpt-signal').value;
  if(!dt){alert('請選擇日期');return}
  const tb=document.getElementById('trpt-body');
  tb.innerHTML='<tr><td colspan="16" class="note">載入中…</td></tr>';
  const url=`/blood-day/report?trade_date=${dt}`+(sig?`&signal=${sig}`:'');
  const r=await fetch(url);
  const j=await r.json();
  if(!j.data||!j.data.length){tb.innerHTML='<tr><td colspan="16" class="note">無資料（該日期尚未分析）</td></tr>';return}
  tb.innerHTML=j.data.map(r=>`<tr>
    <td><b>${r.stock_code}</b></td>
    <td>${r.stock_name||'—'}</td>
    <td>${r.sector||'—'}</td>
    <td class="neg">${fmt(r.change_pct,1)}%</td>
    <td>${fmt(r.volume)}</td>
    <td class="${(r.margin_change||0)<0?'neg':'pos'}">${(r.margin_change||0)<0?'洗出':'增加'}${fmt(Math.abs(r.margin_change||0))}</td>
    <td class="${(r.foreign_net||0)>=0?'pos':'neg'}">${fmt(r.foreign_net)}</td>
    <td class="${(r.trust_net||0)>=0?'pos':'neg'}">${fmt(r.trust_net)}</td>
    <td class="${(r.dealer_net||0)>=0?'pos':'neg'}">${fmt(r.dealer_net)}</td>
    <td>${fmt(r.gov_bank_net)}</td>
    <td>${r.top_broker_1_name?r.top_broker_1_name+'('+fmt(r.top_broker_1_net)+')':'—'}</td>
    <td>${r.top_broker_2_name?r.top_broker_2_name+'('+fmt(r.top_broker_2_net)+')':'—'}</td>
    <td>${fmt(r.absorption_ratio,1)}</td>
    <td>${fmt(r.absorption_score,1)}</td>
    <td>${r.principal_type||'—'}</td>
    <td><span class="pill ${tagClass(r.signal_tag)}">${r.signal_tag||'—'}</span></td>
  </tr>`).join('');
}

loadTop20();
</script>
</body></html>"""
    return HTMLResponse(content=html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)

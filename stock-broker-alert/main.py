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
from fastapi.responses import PlainTextResponse

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)

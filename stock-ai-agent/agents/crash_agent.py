"""
crash_agent — 暴跌日「帶血籌碼承接」分析
=========================================
觸發條件：大盤跌幅 > 2%（TAIEX）
執行流程：
  1. 篩選當日跌幅 > 5% 的個股（最多 50 檔）
  2. 拉取三大法人 + 八大公股 + 外資主力分點
  3. 計算承接評分 (0-10)
  4. 寫入 crash_absorption_events 表
  5. Telegram 推播前 5 名承接訊號

設計原則（不崩潰）：
  - 分點資料：一次一檔，semaphore 限 3 並發（資料量大，一檔 ~1MB）
  - 公股資料：全市場一次 call，本函式收到 dict 直接查找
  - 每次最多分析 50 檔（防止 API 超限）
  - 每檔分析完 sleep 0.3s 讓伺服器緩口氣
"""
import asyncio
import logging
import httpx
import urllib.parse
from datetime import date as _date
from typing import Optional

from utils.db import execute, fetch_all
from utils.finmind_client import (
    fetch_institutional, fetch_margin,
)

logger = logging.getLogger(__name__)

# 外資券商關鍵字（用於辨識分點是否為外資）
_FOREIGN_KEYWORDS = ["高盛", "美林", "摩根", "野村", "瑞銀", "花旗", "匯豐", "德意志",
                     "麥格理", "巴克萊", "法興", "里昂", "港豐", "新加坡", "港商"]
_GOV_BANK_NAMES   = ["兆豐", "合庫", "第一", "華南", "臺銀", "土銀", "彰銀", "台企銀"]

# 分點並發限制（資料量大，不能太高）
_BROKER_SEM = asyncio.Semaphore(3)


def _is_foreign_broker(name: str) -> bool:
    return any(kw in name for kw in _FOREIGN_KEYWORDS)


async def _fetch_broker_data(stock_code: str, trade_date: str, token: str) -> list[dict]:
    """拉取單檔分點資料（TaiwanStockTradingDailyReport）。"""
    async with _BROKER_SEM:
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {
            "dataset": "TaiwanStockTradingDailyReport",
            "data_id": stock_code,
            "start_date": trade_date,
            "token": token,
        }
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(url, params=params)
                r.raise_for_status()
                data = r.json()
                return data.get("data", [])
        except Exception as e:
            logger.warning(f"分點資料失敗 {stock_code}: {e}")
            return []


async def _fetch_gov_bank_all(trade_date: str, token: str) -> dict[str, list[dict]]:
    """
    八大公股全市場資料（單次 API call，效率最高）。
    回傳: {stock_code: [{bank_name, buy, sell, ...}, ...]}
    """
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanStockGovernmentBankBuySell",
        "start_date": trade_date,
        "token": token,
    }
    result: dict[str, list] = {}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            rows = r.json().get("data", [])
        for row in rows:
            code = row.get("stock_id", "")
            if code not in result:
                result[code] = []
            result[code].append(row)
        logger.info(f"八大公股資料：{len(rows)} 筆，涵蓋 {len(result)} 檔")
    except Exception as e:
        logger.warning(f"八大公股資料失敗: {e}")
    return result


def _calc_absorption_score(
    foreign_net: int,
    trust_net: int,
    dealer_net: int,
    gov_bank_net: int,
    top_foreign_broker_net: int,
    margin_change: int,
    drop_pct: float,
    volume_ratio: float,
) -> float:
    """
    承接評分 0-10：
      外資法人淨買  > 0        → +2
      投信淨買      > 0        → +1
      公股合計淨買  > 0        → +2
      外資主力分點  > 1萬張    → +2
      融資未大增（增幅<10%）    → +1
      量能比 > 2（大成交量）    → +1
      跌幅越深但承接越強        → 加成 +1
    """
    total = 0.0

    if foreign_net > 0:    total += 2.0
    if trust_net > 0:      total += 1.0
    if gov_bank_net > 0:   total += 2.0
    if top_foreign_broker_net > 10000: total += 2.0
    elif top_foreign_broker_net > 0:   total += 1.0
    if margin_change < drop_pct * 100: total += 1.0   # 融資沒追高
    if volume_ratio >= 2.0:            total += 1.0

    # 跌深承接加成
    if drop_pct <= -7 and total >= 6: total = min(total + 1.0, 10.0)

    return round(min(total, 10.0), 1)


def _signal_tag(score: float) -> str:
    if score >= 7:   return "STRONG_BUY"
    if score >= 4.5: return "WATCH"
    return "AVOID"


async def analyze_crash_day(trade_date: str, token: str, taiex_drop: float = 0.0) -> list[dict]:
    """
    主入口：分析暴跌日的承接訊號。
    trade_date: "YYYY-MM-DD"
    token: FinMind API token
    taiex_drop: 大盤跌幅（負數），用於觸發判斷（外部已確認 < -2% 才呼叫）
    """
    td = _date.fromisoformat(trade_date)

    # ── 1. 找當日跌幅 > 5% 的個股（從 DB，不重複打 API）────────────────────────
    crash_stocks = await fetch_all("""
        SELECT sp.stock_code, s.stock_name,
               sp.change_pct, sp.volume,
               COALESCE(
                   sp.volume::float / NULLIF(AVG(sp2.volume) OVER (
                       PARTITION BY sp.stock_code
                       ORDER BY sp.trade_date
                       ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
                   ), 0), 1.0
               ) AS volume_ratio
        FROM stock_prices sp
        JOIN stocks s ON s.stock_code = sp.stock_code
        WHERE sp.trade_date = $1 AND sp.change_pct <= -5
        ORDER BY sp.change_pct ASC
        LIMIT 50
    """, td)

    if not crash_stocks:
        logger.info(f"[崩盤分析] {trade_date} 無跌幅 > 5% 個股")
        return []

    logger.info(f"[崩盤分析] {trade_date} 找到 {len(crash_stocks)} 檔跌幅 > 5%，開始承接分析")

    # ── 2. 八大公股（一次 call 全市場）─────────────────────────────────────────
    gov_bank_all = await _fetch_gov_bank_all(trade_date, token)

    # ── 3. 逐檔分析 ──────────────────────────────────────────────────────────
    results = []

    for stock in crash_stocks:
        code      = stock["stock_code"]
        name      = stock["stock_name"]
        drop_pct  = float(stock["change_pct"] or 0)
        vol_ratio = float(stock.get("volume_ratio") or 1.0)

        try:
            # 三大法人（FinMind，已有 semaphore 在 finmind_client）
            inst = await fetch_institutional(code, trade_date)
            if not inst:
                inst = {"foreign_net_buy": 0, "investment_trust_net_buy": 0, "dealer_net_buy": 0}

            # 融資
            mg = await fetch_margin(code, trade_date)
            margin_change = int(mg.get("margin_balance", 0)) if mg else 0

            # 分點資料（有限流）
            broker_rows = await _fetch_broker_data(code, trade_date, token)

            # 外資主力分點
            top_foreign_broker = ""
            top_foreign_net    = 0
            if broker_rows:
                foreign_rows = [r for r in broker_rows if _is_foreign_broker(r.get("securities_trader", ""))]
                if foreign_rows:
                    agg: dict[str, int] = {}
                    for r in foreign_rows:
                        t = r["securities_trader"]
                        agg[t] = agg.get(t, 0) + int(r["buy"] or 0) - int(r["sell"] or 0)
                    top = max(agg.items(), key=lambda x: x[1])
                    top_foreign_broker = top[0]
                    top_foreign_net    = top[1]

            # 八大公股合計
            gov_rows      = gov_bank_all.get(code, [])
            gov_bank_net  = sum(int(r.get("buy", 0)) - int(r.get("sell", 0)) for r in gov_rows)
            gov_detail    = {r["bank_name"]: int(r.get("buy", 0)) - int(r.get("sell", 0))
                             for r in gov_rows}

            # 承接評分
            score = _calc_absorption_score(
                inst["foreign_net_buy"],
                inst["investment_trust_net_buy"],
                gov_bank_net,
                gov_bank_net,
                top_foreign_net,
                margin_change,
                drop_pct,
                vol_ratio,
            )

            event = {
                "stock_code":          code,
                "name":                name,
                "crash_date":          trade_date,
                "drop_pct":            drop_pct,
                "volume_ratio":        vol_ratio,
                "foreign_net":         inst["foreign_net_buy"],
                "trust_net":           inst["investment_trust_net_buy"],
                "dealer_net":          inst["dealer_net_buy"],
                "total_inst_net":      (inst["foreign_net_buy"] + inst["investment_trust_net_buy"]
                                        + inst["dealer_net_buy"]),
                "top_foreign_broker":  top_foreign_broker,
                "top_foreign_net":     top_foreign_net,
                "gov_bank_net":        gov_bank_net,
                "gov_bank_detail":     gov_detail,
                "margin_change":       margin_change,
                "absorption_score":    score,
                "signal_tag":          _signal_tag(score),
            }
            results.append(event)

            # 寫入 DB
            import json as _json
            await execute("""
                INSERT INTO crash_absorption_events
                    (stock_code, crash_date, drop_pct, volume_ratio,
                     foreign_net, trust_net, dealer_net, total_inst_net,
                     top_foreign_broker, top_foreign_net,
                     gov_bank_net, gov_bank_detail,
                     margin_change, absorption_score, signal_tag)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14,$15)
                ON CONFLICT (stock_code, crash_date) DO UPDATE SET
                    absorption_score = EXCLUDED.absorption_score,
                    signal_tag       = EXCLUDED.signal_tag,
                    top_foreign_net  = EXCLUDED.top_foreign_net,
                    gov_bank_net     = EXCLUDED.gov_bank_net,
                    gov_bank_detail  = EXCLUDED.gov_bank_detail
            """, code, td, drop_pct, vol_ratio,
                 inst["foreign_net_buy"], inst["investment_trust_net_buy"],
                 inst["dealer_net_buy"],
                 inst["foreign_net_buy"] + inst["investment_trust_net_buy"] + inst["dealer_net_buy"],
                 top_foreign_broker, top_foreign_net,
                 gov_bank_net, _json.dumps(gov_detail, ensure_ascii=False),
                 margin_change, score, _signal_tag(score))

            logger.info(f"[崩盤] {code} {name}: 跌{drop_pct:.1f}% 承接{score:.1f}分 {_signal_tag(score)}")
            await asyncio.sleep(0.3)  # 讓 API 喘口氣

        except Exception as e:
            logger.warning(f"[崩盤] {code} 分析失敗: {e}")

    # 排序：承接評分由高到低
    results.sort(key=lambda x: x["absorption_score"], reverse=True)
    return results


def build_crash_telegram_msg(results: list[dict], trade_date: str, taiex_drop: float) -> str:
    """組裝 Telegram 推播訊息。"""
    lines = [
        f"🚨 *暴跌日帶血籌碼承接報告 {trade_date}*",
        f"大盤跌幅：{taiex_drop:.1f}%",
        f"分析檔數：{len(results)} 檔",
        "",
    ]
    top5 = [r for r in results if r["signal_tag"] in ("STRONG_BUY", "WATCH")][:5]
    if not top5:
        lines.append("今日無強力承接訊號（市場恐慌性拋售為主）")
    else:
        lines.append("📌 *強力承接股（評分由高至低）*")
        for r in top5:
            tag = "⭐強買" if r["signal_tag"] == "STRONG_BUY" else "👀觀察"
            gov = f" 公股:{r['gov_bank_net']:+,}" if r['gov_bank_net'] != 0 else ""
            lines.append(
                f"{tag} `{r['stock_code']}` {r['name']}  "
                f"跌{r['drop_pct']:.1f}% "
                f"評分{r['absorption_score']:.1f}/10\n"
                f"    外資:{r['foreign_net']:+,} 投信:{r['trust_net']:+,}{gov}\n"
                f"    主力分點: {r['top_foreign_broker'] or '—'} {r['top_foreign_net']:+,}"
            )
    return "\n".join(lines)

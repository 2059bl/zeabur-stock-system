"""
Broker Score 計算 + 隔日沖行為分析 + Watchlist 維護
"""
import asyncio
import logging
from datetime import date, timedelta
from collections import defaultdict

from src.db import execute, fetch_all, fetch_one

logger = logging.getLogger(__name__)

# ─── Score 配分 ──────────────────────────────────────────────────────────────
_S_BLOOD_DAY      = 20  # 暴跌日承接
_S_HIGH_ABS       = 20  # 吞噬率 >= 15%
_S_CONSECUTIVE    = 20  # 連續多日承接（3日以上）
_S_MULTI_SECTOR   = 15  # 同族群多檔承接（3檔以上）
_S_INST_SYNC      = 15  # 法人同步買超
_S_DAY_TRADE      = -20 # 高頻隔日沖懲罰
_WATCHLIST_ENTRY  = 50  # 進入 watchlist 最低分


async def calc_day_trade_score(
    broker_code: str,
    reference_date: str,
    lookback: int = 20,
) -> dict:
    """
    計算隔日沖分數（0-100，越高越像隔日沖）。
    分析邏輯：今日大量買進 → 次日大量賣出 的頻率。
    """
    ref = date.fromisoformat(reference_date)
    start = (ref - timedelta(days=lookback * 2)).isoformat()

    # 取 broker 近期每日動作
    rows = await fetch_all(
        """
        SELECT trade_date, stock_code, buy_volume, sell_volume, net_volume
        FROM broker_daily_actions
        WHERE broker_code=$1 AND trade_date >= $2
        ORDER BY trade_date ASC, stock_code
        """,
        broker_code, date.fromisoformat(start),
    )

    if not rows:
        return {"day_trade_score": 0, "day_trade_risk": "LOW",
                "day_trade_detail": "INSUFFICIENT_DATA"}

    # 找出「今買多 → 明賣多」的事件
    by_stock: dict[str, list] = defaultdict(list)
    for r in rows:
        by_stock[r["stock_code"]].append(r)

    dt_events = 0
    total_pairs = 0
    for code, actions in by_stock.items():
        for i in range(len(actions) - 1):
            cur  = actions[i]
            nxt  = actions[i+1]
            # 確認是連續交易日（差一天，或跳過週末）
            d_cur = cur["trade_date"]
            d_nxt = nxt["trade_date"]
            if abs((d_nxt - d_cur).days) > 4:
                continue
            if cur["net_volume"] > 500 and nxt["net_volume"] < -500:
                dt_events += 1
            total_pairs += 1

    if total_pairs == 0:
        score = 0.0
        freq  = 0.0
    else:
        freq  = dt_events / total_pairs
        score = min(freq * 100, 100)

    risk  = "HIGH" if score >= 60 else ("MEDIUM" if score >= 30 else "LOW")
    detail = f"隔日沖事件 {dt_events}/{total_pairs}，頻率 {freq:.1%}"

    # 寫入 day_trade_history
    for code, actions in by_stock.items():
        for a in actions:
            try:
                await execute(
                    """
                    INSERT INTO day_trade_history(broker_code, stock_code, buy_date, buy_volume, sell_volume)
                    VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING
                    """,
                    broker_code, code, a["trade_date"],
                    a["buy_volume"], a["sell_volume"],
                )
            except Exception:
                pass

    return {"day_trade_score": round(score, 1), "day_trade_risk": risk, "day_trade_detail": detail}


async def calc_broker_score(broker_code: str, reference_date: str) -> int:
    """計算單一分點的 broker_score。"""
    ref   = date.fromisoformat(reference_date)
    start = (ref - timedelta(days=90)).isoformat()

    actions = await fetch_all(
        """SELECT trade_date, stock_code, net_volume, absorption_ratio, is_blood_day
           FROM broker_daily_actions
           WHERE broker_code=$1 AND trade_date >= $2""",
        broker_code, date.fromisoformat(start),
    )
    if not actions:
        return 0

    score = 0

    # 暴跌日承接
    blood_days = sum(1 for a in actions if a["is_blood_day"] and a["net_volume"] > 0)
    if blood_days >= 1:
        score += _S_BLOOD_DAY

    # 高吞噬率
    high_abs = any((a.get("absorption_ratio") or 0) >= 15 for a in actions)
    if high_abs:
        score += _S_HIGH_ABS

    # 連續多日承接（同一股票 3 日以上）
    by_stock: dict[str, list] = defaultdict(list)
    for a in actions:
        by_stock[a["stock_code"]].append(a)

    max_consec = 0
    for code, acs in by_stock.items():
        acs_sorted = sorted(acs, key=lambda x: x["trade_date"])
        consec = 1
        for i in range(1, len(acs_sorted)):
            if acs_sorted[i]["net_volume"] > 0 and acs_sorted[i-1]["net_volume"] > 0:
                consec += 1
                max_consec = max(max_consec, consec)
            else:
                consec = 1

    if max_consec >= 3:
        score += _S_CONSECUTIVE

    # 同族群多檔承接
    distinct_stocks = len({a["stock_code"] for a in actions if a["net_volume"] > 0})
    if distinct_stocks >= 3:
        score += _S_MULTI_SECTOR

    # 法人同步（從 institutional_daily 看外資/投信是否同日買超）
    blood_dates = {a["trade_date"] for a in actions if a["is_blood_day"] and a["net_volume"] > 0}
    inst_sync_count = 0
    for bd in blood_dates:
        stocks_on_day = [a["stock_code"] for a in actions
                         if a["trade_date"] == bd and a["net_volume"] > 0]
        for sc in stocks_on_day[:3]:
            row = await fetch_one(
                "SELECT foreign_net FROM institutional_daily WHERE stock_code=$1 AND trade_date=$2",
                sc, bd,
            )
            if row and (row.get("foreign_net") or 0) > 0:
                inst_sync_count += 1
                break

    if inst_sync_count >= 1:
        score += _S_INST_SYNC

    # 隔日沖懲罰
    dt = await calc_day_trade_score(broker_code, reference_date)
    if dt["day_trade_risk"] == "HIGH":
        score += _S_DAY_TRADE

    return max(score, 0)


async def update_watchlist(
    broker_code: str,
    broker_name: str,
    reference_date: str,
    detected_stocks: list[str],
    detected_sectors: list[str],
    net_buy: int,
    max_abs: float,
    blood_date: str,
):
    """upsert broker_watchlist。"""
    score = await calc_broker_score(broker_code, reference_date)
    dt    = await calc_day_trade_score(broker_code, reference_date)

    existing = await fetch_one(
        "SELECT id, blood_selling_dates, detected_stocks, total_net_buy FROM broker_watchlist WHERE broker_code=$1",
        broker_code,
    )

    if existing:
        old_dates   = list(existing.get("blood_selling_dates") or [])
        old_stocks  = list(existing.get("detected_stocks") or [])
        new_dates   = list(set(old_dates + [date.fromisoformat(blood_date)]))
        new_stocks  = list(set(old_stocks + detected_stocks))
        await execute(
            """
            UPDATE broker_watchlist SET
              broker_name=$1, detected_stocks=$2, detected_sectors=$3,
              total_net_buy=total_net_buy+$4,
              max_absorption_ratio=GREATEST(max_absorption_ratio,$5),
              blood_selling_dates=$6, blood_selling_count=array_length($6::date[],1),
              broker_score=$7, day_trade_score=$8, day_trade_risk=$9,
              active=TRUE, updated_at=NOW()
            WHERE broker_code=$10
            """,
            broker_name, new_stocks, detected_sectors, net_buy, max_abs,
            new_dates, score,
            dt["day_trade_score"], dt["day_trade_risk"], broker_code,
        )
    else:
        if score < _WATCHLIST_ENTRY:
            return  # 分數未達門檻，不加入 watchlist
        await execute(
            """
            INSERT INTO broker_watchlist
              (broker_code, broker_name, first_detected_date, detected_stocks, detected_sectors,
               total_net_buy, max_absorption_ratio, blood_selling_dates, blood_selling_count,
               broker_score, day_trade_score, day_trade_risk, active)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,TRUE)
            """,
            broker_code, broker_name,
            date.fromisoformat(blood_date), detected_stocks, detected_sectors,
            net_buy, max_abs,
            [date.fromisoformat(blood_date)], 1,
            score, dt["day_trade_score"], dt["day_trade_risk"],
        )


async def rebuild_watchlist_from_history():
    """
    從歷史 broker_daily_actions 重建 watchlist
    （初次執行或重刷時使用）。
    """
    logger.info("重建 Broker Watchlist 從歷史資料...")
    rows = await fetch_all(
        """
        SELECT broker_code, broker_name,
               MIN(trade_date) AS first_date,
               array_agg(DISTINCT stock_code) AS stocks,
               SUM(GREATEST(net_volume,0)) AS total_net,
               MAX(COALESCE(absorption_ratio,0)) AS max_abs,
               array_agg(DISTINCT trade_date) FILTER (WHERE is_blood_day) AS blood_dates
        FROM broker_daily_actions
        WHERE net_volume > 0
        GROUP BY broker_code, broker_name
        HAVING SUM(GREATEST(net_volume,0)) > 1000
        ORDER BY total_net DESC
        LIMIT 100
        """
    )

    for r in rows:
        code       = r["broker_code"]
        name       = r["broker_name"] or code
        stocks     = list(r["stocks"] or [])
        blood      = [str(d) for d in (r["blood_dates"] or []) if d]
        if not blood:
            continue
        await update_watchlist(
            code, name, str(date.today()),
            stocks, [], r["total_net"], r["max_abs"],
            blood[0],
        )

    count = await fetch_all("SELECT COUNT(*) AS cnt FROM broker_watchlist WHERE active=TRUE")
    logger.info(f"Watchlist 重建完成：{count[0]['cnt'] if count else 0} 個追蹤分點")

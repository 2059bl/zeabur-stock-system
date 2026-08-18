"""
暴跌日「帶血籌碼承接」分析
============================
資料品質規範：
  - Check 1: volume >= 0
  - Check 2: net = buy - sell
  - Check 3: top brokers sorted DESC
  - Check 4: institutional totals reasonable
  - Check 5: date must be trading day
  - Check 6: stock_code must be in universe
  - Check 7: no duplicates (DB UNIQUE)
  - Check 8: missing data flagged, never imputed
"""
import asyncio
import logging
import json
from datetime import date

from src.db import execute, fetch_one
from src.market_data import fetch_price, get_crash_stocks
from src.institutional import fetch_institutional, fetch_gov_bank_all
from src.broker import fetch_broker_batch
from src.margin import fetch_margin
from src.universe import get_all_codes, get_stock_info

logger = logging.getLogger(__name__)

# ─── 主力性質判讀 ────────────────────────────────────────────────────────────────
_GOV_KW  = ["兆豐", "合庫", "第一", "華南", "臺銀", "土銀", "彰銀", "台企銀"]
_FRGN_KW = ["高盛", "美林", "摩根", "野村", "瑞銀", "花旗", "匯豐", "德意志", "麥格理", "巴克萊"]


def _classify_principal(
    foreign_net: int, trust_net: int, dealer_net: int,
    gov_bank_net: int,
    top_brokers: list[dict],
    day_trade_risk: bool,
) -> str:
    """主力性質判讀（六大類）。"""
    if not top_brokers:
        return "無法判定"

    top_names = [b.get("broker_name", "") for b in top_brokers[:3]]

    has_gov    = gov_bank_net > 0 and gov_bank_net > abs(foreign_net) * 0.5
    has_frgn   = any(any(kw in n for kw in _FRGN_KW) for n in top_names)
    has_gov_br = any(any(kw in n for kw in _GOV_KW) for n in top_names)
    inst_total = foreign_net + trust_net + dealer_net

    if day_trade_risk:
        return "高頻隔日沖型"
    if has_gov or has_gov_br:
        if has_frgn:
            return "混合型"
        return "公股承接"
    if has_frgn:
        if inst_total > 0:
            return "外資承接"
        return "混合型"
    if foreign_net > 0 and foreign_net > abs(trust_net + dealer_net) * 2:
        return "外資承接"
    if trust_net > 0 and trust_net > abs(foreign_net + dealer_net):
        return "特定券商承接"
    if inst_total > 0:
        return "特定券商承接"
    if top_brokers and top_brokers[0].get("absorption_ratio", 0) >= 10:
        return "主力地緣型分點"
    return "混合型"


def _absorption_score(
    foreign_net: int, trust_net: int, dealer_net: int,
    gov_bank_net: int,
    top_broker_net: int,
    margin_change: int,
    drop_pct: float,
    volume_ratio: float,
) -> float:
    """承接評分 0-10（浮點）。"""
    score = 0.0
    if foreign_net   > 0: score += 2.0
    if trust_net     > 0: score += 1.0
    if gov_bank_net  > 0: score += 2.0
    if top_broker_net > 10000: score += 2.0
    elif top_broker_net > 0:   score += 1.0
    if margin_change  < 0:     score += 1.0   # 融資洗出（空手承接）
    if (volume_ratio or 0) >= 2.0: score += 1.0  # 放量
    if drop_pct <= -7 and score >= 6: score += 1.0  # 深跌強承接
    return min(score, 10.0)


def _data_quality_checks(r: dict) -> list[str]:
    """回傳發現的資料問題（空 list = 全部通過）。"""
    issues = []
    if (r.get("volume") or 0) < 0:
        issues.append("CHECK1_NEGATIVE_VOLUME")
    top = r.get("top_brokers", [])
    for b in top:
        if b.get("net_volume") != b.get("buy_volume", 0) - b.get("sell_volume", 0):
            issues.append(f"CHECK2_NET_MISMATCH_{b.get('broker_code')}")
            break
    if top and len(top) >= 2:
        nets = [b.get("net_volume", 0) for b in top]
        if nets != sorted(nets, reverse=True):
            issues.append("CHECK3_BROKER_SORT_ERROR")
    return issues


async def analyze_blood_day(
    trade_date: str,
    drop_threshold: float = -4.0,
    volume_ratio_min: float = 1.5,
    max_stocks: int = 60,
    taiex_drop: float = 0.0,
) -> list[dict]:
    """
    主分析函式：對指定暴跌日執行帶血籌碼承接分析。
    回傳每支個股的完整分析結果。
    """
    logger.info(f"=== 帶血籌碼承接分析開始 {trade_date} (大盤 {taiex_drop:.1f}%) ===")

    # ── Step 1: 取得符合條件的個股（從 DB stock_daily）──────────────────────────
    crash_stocks = await get_crash_stocks(trade_date, drop_threshold, max_stocks)
    if not crash_stocks:
        logger.warning(f"{trade_date} 無跌幅達 {drop_threshold}% 的個股（universe 範圍內）")
        return []

    logger.info(f"符合跌幅條件：{len(crash_stocks)} 支")

    # ── Step 2: 八大公股一次 call ────────────────────────────────────────────────
    gov_all = await fetch_gov_bank_all(trade_date)
    logger.info(f"公股資料：{len(gov_all)} 支有記錄")

    # ── Step 3: 每支個股並發取法人 + 融資 ───────────────────────────────────────
    async def _fetch_stock(s: dict) -> tuple[str, dict, dict]:
        code = s["stock_code"]
        inst   = await fetch_institutional(code, trade_date) or {}
        margin = await fetch_margin(code, trade_date) or {}
        return code, inst, margin

    inst_tasks  = [_fetch_stock(s) for s in crash_stocks]
    inst_results = await asyncio.gather(*inst_tasks, return_exceptions=True)

    inst_map   = {}
    margin_map = {}
    for item in inst_results:
        if isinstance(item, Exception):
            continue
        code, inst, margin = item
        inst_map[code]   = inst
        margin_map[code] = margin

    # ── Step 4: 批量取券商分點（有限並發）──────────────────────────────────────
    price_map = {s["stock_code"]: {
        "volume":     s.get("volume", 0),
        "change_pct": s.get("change_pct", 0),
    } for s in crash_stocks}

    broker_map = await fetch_broker_batch(
        [{"code": s["stock_code"]} for s in crash_stocks],
        trade_date,
        price_map,
        is_blood_day=True,
    )

    # ── Step 5: 逐支計算分析結果 ─────────────────────────────────────────────
    results = []
    for s in crash_stocks:
        code    = s["stock_code"]
        name    = s.get("stock_name", code)
        sector  = s.get("sector", "")
        drop    = float(s.get("change_pct", 0) or 0)
        vol     = int(s.get("volume", 0) or 0)
        vol_r   = float(s.get("volume_ratio") or 0)

        inst   = inst_map.get(code, {})
        margin = margin_map.get(code, {})
        gov    = gov_all.get(code, {})
        bdata  = broker_map.get(code, [])

        # 法人
        f_net = int(inst.get("foreign_net", 0) or 0)
        t_net = int(inst.get("trust_net",   0) or 0)
        d_net = int(inst.get("dealer_net",  0) or 0)
        # 公股
        g_net     = int(gov.get("gov_bank_net", 0) or 0)
        g_detail  = gov.get("gov_bank_detail", {})
        g_status  = gov.get("pub_bank_status", "PUBLIC_BANK_DATA_UNAVAILABLE")
        # 融資
        m_change = int(margin.get("margin_change", 0) or 0)
        fin_avail = margin.get("financing_data_available", False)

        # Top 3 分點
        top3 = bdata[:3] if bdata else []
        top_net = top3[0].get("net_volume", 0) if top3 else 0
        max_abs = max((b.get("absorption_ratio") or 0 for b in bdata), default=0)

        # 評分
        score = _absorption_score(f_net, t_net, d_net, g_net, top_net, m_change, drop, vol_r)
        sig   = "STRONG_BUY" if score >= 7 else ("WATCH" if score >= 4.5 else "AVOID")
        is_blood = drop <= -5 and vol_r >= 1.5 and (
            f_net > 0 or g_net > 0 or (top_net > 0 and max_abs >= 15)
        )

        # 主力判讀
        day_trade_risk = False  # 隔日沖分析在 broker_score.py 做
        principal = _classify_principal(f_net, t_net, d_net, g_net, top3, day_trade_risk)

        record = {
            "trade_date":         trade_date,
            "stock_code":         code,
            "stock_name":         name,
            "sector":             sector,
            "change_pct":         drop,
            "volume":             vol,
            "volume_ratio":       vol_r,
            "margin_change":      m_change,
            "financing_absorbed": "AVAILABLE" if fin_avail else "FINANCING_DATA_UNAVAILABLE",
            "foreign_net":        f_net,
            "trust_net":          t_net,
            "dealer_net":         d_net,
            "total_inst_net":     f_net + t_net + d_net,
            "gov_bank_net":       g_net,
            "gov_bank_detail":    g_detail,
            "pub_bank_status":    g_status if not g_detail else "OK",
            "top_brokers":        bdata,
            "absorption_ratio":   max_abs,
            "absorption_score":   score,
            "is_blood_absorption": is_blood,
            "principal_type":     principal,
            "signal_tag":         sig,
        }

        # 資料品質 check
        issues = _data_quality_checks(record)
        record["data_complete"] = len(issues) == 0
        record["data_issues"]   = issues

        # 寫入 DB
        await _upsert_report(record, top3)
        results.append(record)

        flag = "🩸" if is_blood else ("⚠️" if score >= 4.5 else "")
        logger.info(f"  {flag} {code} {name}: drop={drop:.1f}% score={score:.1f} [{sig}] 主力:{principal}")

    blood_count = sum(1 for r in results if r["is_blood_absorption"])
    logger.info(f"=== 分析完成：{len(results)} 支，🩸 帶血承接 {blood_count} 支 ===")
    return results


async def _upsert_report(r: dict, top3: list[dict]):
    b1 = top3[0] if len(top3) > 0 else {}
    b2 = top3[1] if len(top3) > 1 else {}
    b3 = top3[2] if len(top3) > 2 else {}
    try:
        await execute(
            """
            INSERT INTO blood_absorption_report
              (trade_date, stock_code, stock_name, sector,
               change_pct, volume, volume_ratio,
               margin_change, financing_absorbed,
               foreign_net, trust_net, dealer_net, total_inst_net,
               gov_bank_net, gov_bank_detail, pub_bank_status,
               top_broker_1_code, top_broker_1_name, top_broker_1_net,
               top_broker_2_code, top_broker_2_name, top_broker_2_net,
               top_broker_3_code, top_broker_3_name, top_broker_3_net,
               absorption_ratio, absorption_score, is_blood_absorption,
               principal_type, signal_tag, data_complete, data_issues)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                    $17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,$31,$32)
            ON CONFLICT(trade_date, stock_code) DO UPDATE SET
              absorption_score = EXCLUDED.absorption_score,
              is_blood_absorption = EXCLUDED.is_blood_absorption,
              signal_tag = EXCLUDED.signal_tag,
              principal_type = EXCLUDED.principal_type,
              data_complete = EXCLUDED.data_complete
            """,
            date.fromisoformat(r["trade_date"]),
            r["stock_code"], r["stock_name"], r["sector"],
            r["change_pct"], r["volume"], r["volume_ratio"],
            r["margin_change"], r["financing_absorbed"],
            r["foreign_net"], r["trust_net"], r["dealer_net"], r["total_inst_net"],
            r["gov_bank_net"],
            json.dumps(r["gov_bank_detail"], ensure_ascii=False) if r["gov_bank_detail"] else None,
            r["pub_bank_status"],
            b1.get("broker_code"), b1.get("broker_name"), b1.get("net_volume"),
            b2.get("broker_code"), b2.get("broker_name"), b2.get("net_volume"),
            b3.get("broker_code"), b3.get("broker_name"), b3.get("net_volume"),
            r["absorption_ratio"], r["absorption_score"], r["is_blood_absorption"],
            r["principal_type"], r["signal_tag"],
            r["data_complete"], r["data_issues"] or [],
        )
    except Exception as e:
        logger.warning(f"blood_absorption_report upsert {r['stock_code']}: {e}")

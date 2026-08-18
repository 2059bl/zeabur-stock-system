"""
Telegram 推播 — 帶血籌碼承接警報
"""
import os
import logging
import urllib.parse
import urllib.request
from datetime import datetime

logger = logging.getLogger(__name__)

TG_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def _send(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT_ID:
        logger.warning("Telegram 未設定，跳過推播")
        return False
    try:
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id":    TG_CHAT_ID,
            "text":       text,
            "parse_mode": "HTML",
        }).encode()
        urllib.request.urlopen(url, data, timeout=10)
        return True
    except Exception as e:
        logger.warning(f"Telegram 推播失敗：{e}")
        return False


def fmt_alert_abnormal_buy(
    broker_code: str,
    broker_name: str,
    stock_code: str,
    stock_name: str,
    trade_date: str,
    net_volume: int,
    absorption_pct: float,
    net_amount: float,
    broker_score: int,
    principal_type: str,
    history_dates: list[str],
    consecutive_days: int = 1,
    total_buy_consec: int = 0,
) -> str:
    history_str = "、".join(history_dates[:3]) if history_dates else "首次偵測"
    strategy = (
        "⚠️ 防範隔日沖倒貨" if "隔日沖" in principal_type
        else "🎯 卡位跟蹤觀察" if broker_score >= 70
        else "📊 偏多觀察"
    )
    amount_str = f"NT$ {net_amount:,.0f}" if net_amount else "—"
    return (
        f"⚠️【關鍵分點動向警報】\n"
        f"關鍵分點： {broker_name} / {broker_code}\n"
        f"標的： {stock_code} {stock_name}\n"
        f"日期： {trade_date}\n"
        f"當日動作： 買超 {net_volume:,} 張\n"
        f"占成交量： {absorption_pct:.2f}%\n"
        f"買超金額： {amount_str}\n"
        f"警報類型： 異常大買\n"
        f"歷史淵源： 曾於 {history_str} 暴跌日承接相關股票。\n"
        f"近期累計： 連續 {consecutive_days} 日買超 累計買超 {total_buy_consec:,} 張\n"
        f"主力判讀： {principal_type}\n"
        f"策略提示： {strategy}"
    )


def fmt_alert_consecutive(
    broker_code: str,
    broker_name: str,
    stock_code: str,
    stock_name: str,
    trade_date: str,
    days: int,
    day_details: list[dict],   # [{date, net_vol, amount}]
    total_net: int,
    total_amount: float,
    avg_cost: float | None,
    broker_score: int,
    principal_type: str,
    history_dates: list[str],
) -> str:
    detail_lines = ""
    for i, d in enumerate(day_details[:3], 1):
        detail_lines += (
            f"  第{i}天 ({d.get('date')})：買超 {d.get('net_vol', 0):,} 張\n"
        )
    avg_str = f"NT$ {avg_cost:.1f}" if avg_cost else "—"
    history_str = "、".join(history_dates[:3]) if history_dates else "首次偵測"
    strategy = (
        "⚠️ 防範隔日沖倒貨" if "隔日沖" in principal_type
        else "🔥 強力卡位，持續跟蹤" if broker_score >= 70
        else "📊 偏多觀察，留意轉折"
    )
    return (
        f"🔥【關鍵分點連續佈局警報】\n"
        f"關鍵分點： {broker_name} / {broker_code}\n"
        f"標的： {stock_code} {stock_name}\n"
        f"日期： {trade_date}（第 {days} 日）\n"
        f"警報類型： 連續佈局\n"
        f"{detail_lines}"
        f"累計買超：{total_net:,} 張\n"
        f"平均成本：{avg_str}\n"
        f"歷史淵源： 曾於 {history_str} 暴跌日承接相關股票。\n"
        f"主力判讀： {principal_type}\n"
        f"策略提示： {strategy}"
    )


def fmt_alert_day_trade_risk(
    broker_code: str,
    broker_name: str,
    stock_code: str,
    stock_name: str,
    trade_date: str,
    day_trade_score: float,
    dt_detail: str,
) -> str:
    return (
        f"⚠️【隔日沖風險警示】\n"
        f"關鍵分點： {broker_name} / {broker_code}\n"
        f"標的： {stock_code} {stock_name}\n"
        f"日期： {trade_date}\n"
        f"警報類型： 隔日沖風險\n"
        f"隔日沖分數： {day_trade_score:.0f}/100\n"
        f"分析明細： {dt_detail}\n"
        f"策略提示： ⚠️ 防範隔日沖倒貨，勿盲目跟進"
    )


def fmt_blood_absorption_summary(
    trade_date: str,
    results: list[dict],
    taiex_drop: float,
) -> str:
    """每日暴跌日分析摘要推播。"""
    blood = [r for r in results if r.get("is_blood_absorption")]
    watch = [r for r in results if not r.get("is_blood_absorption") and r.get("absorption_score", 0) >= 4.5]

    now = datetime.now().strftime("%m/%d %H:%M")
    lines = [
        f"🩸【暴跌日籌碼承接分析 {trade_date}】",
        f"大盤跌幅：{taiex_drop:.1f}%  |  分析完成時間：{now}",
        f"分析範圍：{len(results)} 支成長型電子股",
        f"—" * 24,
        f"🩸 帶血籌碼承接：{len(blood)} 支",
    ]
    for r in sorted(blood, key=lambda x: x.get("absorption_score", 0), reverse=True)[:5]:
        lines.append(
            f"  {r['stock_code']} {r.get('stock_name','')} | "
            f"跌{r['change_pct']:.1f}% | 評分{r['absorption_score']:.1f} | {r['principal_type']}"
        )
    if watch:
        lines.append(f"⚠️ 待觀察：{len(watch)} 支")
        for r in sorted(watch, key=lambda x: x.get("absorption_score", 0), reverse=True)[:3]:
            lines.append(f"  {r['stock_code']} {r.get('stock_name','')} | 評分{r['absorption_score']:.1f}")

    return "\n".join(lines)


def send_blood_summary(trade_date: str, results: list[dict], taiex_drop: float) -> bool:
    msg = fmt_blood_absorption_summary(trade_date, results, taiex_drop)
    return _send(msg)


def send_alert(msg: str) -> bool:
    return _send(msg)


def test_telegram() -> bool:
    """測試 Telegram 連線。"""
    return _send(f"✅ stock-broker-alert 系統啟動測試 {datetime.now().strftime('%Y/%m/%d %H:%M')}")

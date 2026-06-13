from dataclasses import dataclass
from utils.db import fetch_all, execute
from utils.finmind_client import fetch_futures_institutional


@dataclass
class BearMarketSignal:
    total_score: float
    level: str
    action: str
    institution_flow: str
    futures_foreign_net: int = 0
    foreign_sell_days: int = 0


async def compute_market_bear_signal(trade_date: str | None = None) -> BearMarketSignal:
    rows = await fetch_all(
        "SELECT * FROM market_indicators ORDER BY trade_date DESC LIMIT 20"
    )

    # 嘗試從 FinMind 取今日期貨法人部位
    futures_data = None
    if trade_date:
        futures_data = await fetch_futures_institutional(trade_date)
        if futures_data:
            # 寫入 market_indicators
            from datetime import date as _date
            td = _date.fromisoformat(trade_date)
            await execute("""
                INSERT INTO market_indicators (trade_date, futures_net_short)
                VALUES ($1, $2)
                ON CONFLICT (trade_date) DO UPDATE SET
                    futures_net_short = EXCLUDED.futures_net_short
            """, td, -futures_data["futures_foreign_net"])

    if not rows:
        return BearMarketSignal(0, "NORMAL", "無大盤數據，維持觀望", "HOLD_OR_BUY")

    latest = rows[0]
    sell_days = latest.get("foreign_sell_days") or 0
    net_short = latest.get("futures_net_short") or 0
    f_net     = latest.get("foreign_net_total") or 0
    tr_net    = latest.get("trust_net_total") or 0
    double_sell = f_net < 0 and tr_net < 0

    # 外資期貨淨空單加重評分
    futures_net = futures_data["futures_foreign_net"] if futures_data else 0
    futures_short_penalty = max(0, -futures_net / 1000) * 5  # 每淨空 1000 口 +5 分

    score = min(100, (sell_days * 12) + (abs(net_short) / 600) + futures_short_penalty)

    if score >= 75:
        level, action = "EXTREME", "全面降低風險部位"
    elif score >= 55:
        level, action = "DANGER", "分批減碼防禦"
    elif score >= 35:
        level, action = "WARNING", "嚴格控管停損"
    else:
        level, action = "NORMAL", "維持既有策略"

    if double_sell:
        flow = "DOUBLE_SELL"
    elif f_net < 0 or tr_net < 0:
        flow = "SINGLE_SELL"
    else:
        flow = "HOLD_OR_BUY"

    return BearMarketSignal(
        round(score, 2), level, action, flow,
        futures_foreign_net=futures_net,
        foreign_sell_days=sell_days,
    )

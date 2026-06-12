from dataclasses import dataclass
from utils.db import fetch_all


@dataclass
class BearMarketSignal:
    total_score: float
    level: str
    action: str
    institution_flow: str


async def compute_market_bear_signal() -> BearMarketSignal:
    rows = await fetch_all(
        "SELECT * FROM market_indicators ORDER BY trade_date DESC LIMIT 20"
    )
    if not rows:
        return BearMarketSignal(0, "NORMAL", "無大盤數據，維持觀望", "HOLD_OR_BUY")

    latest = rows[0]
    sell_days = latest.get("foreign_sell_days") or 0
    net_short = latest.get("futures_net_short") or 0
    f_net = latest.get("foreign_net_total") or 0
    tr_net = latest.get("trust_net_total") or 0
    double_sell = f_net < 0 and tr_net < 0

    score = min(100, (sell_days * 12) + (abs(net_short) / 600))

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

    return BearMarketSignal(round(score, 2), level, action, flow)

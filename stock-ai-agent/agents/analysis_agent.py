import logging
from ..utils.db import fetch_all, execute
from ..utils.indicators import compute_all

logger = logging.getLogger(__name__)


async def run_analysis(stock_code: str, trade_date: str) -> dict:
    prices = await fetch_all("""
        SELECT close_price, high_price, low_price, open_price
        FROM stock_prices
        WHERE stock_code = $1 AND trade_date <= $2
        ORDER BY trade_date ASC LIMIT 150
    """, stock_code, trade_date)

    if len(prices) < 20:
        return {"status": "insufficient_data", "stock_code": stock_code}

    ind = compute_all(prices)

    await execute("""
        INSERT INTO stock_indicators
            (stock_code, trade_date, sma_5, sma_20, sma_60, sma_120,
             rsi_14, macd, macd_signal, macd_histogram, bias_rate, short_trend_confirmed)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
        ON CONFLICT (stock_code, trade_date) DO UPDATE SET
            sma_5              = EXCLUDED.sma_5,
            sma_20             = EXCLUDED.sma_20,
            sma_60             = EXCLUDED.sma_60,
            sma_120            = EXCLUDED.sma_120,
            rsi_14             = EXCLUDED.rsi_14,
            macd               = EXCLUDED.macd,
            macd_signal        = EXCLUDED.macd_signal,
            macd_histogram     = EXCLUDED.macd_histogram,
            bias_rate          = EXCLUDED.bias_rate,
            short_trend_confirmed = EXCLUDED.short_trend_confirmed,
            updated_at         = NOW()
    """, stock_code, trade_date,
         ind["sma_5"], ind["sma_20"], ind["sma_60"], ind["sma_120"],
         ind["rsi_14"], ind["macd"], ind["macd_signal"], ind["macd_histogram"],
         ind["bias_rate"], ind["short_trend_confirmed"])

    return {"status": "ok", "stock_code": stock_code, "indicators": ind}


def generate_text_report(stock_code: str, ind: dict) -> str:
    trend = "空頭確立" if ind.get("short_trend_confirmed") else "整理盤整"
    rsi_val = ind.get("rsi_14")
    rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "N/A"
    return (
        f"【個股代碼】{stock_code}\n"
        f"【均線型態】{trend}  5MA:{ind.get('sma_5')}  20MA:{ind.get('sma_20')}  60MA:{ind.get('sma_60')}\n"
        f"【動能指標】RSI(14):{rsi_str}  MACD柱狀:{ind.get('macd_histogram')}  20日乖離率:{ind.get('bias_rate')}%"
    )

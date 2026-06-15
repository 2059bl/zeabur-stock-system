import numpy as np
from typing import Optional


def sma(prices: list[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return float(np.mean(prices[-period:]))


def ema_series(prices: list[float], period: int) -> list[float]:
    if not prices:
        return []
    k = 2.0 / (period + 1)
    ema_vals = [prices[0]]
    for p in prices[1:]:
        ema_vals.append(p * k + ema_vals[-1] * (1 - k))
    return ema_vals


def rsi(prices: list[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    return float(100 - 100 / (1 + (avg_gain / avg_loss)))


def macd_fixed(prices: list[float], fast=12, slow=26, signal=9) -> dict:
    if len(prices) < slow + signal:
        return {"macd": None, "signal": None, "histogram": None}
    ema_fast = ema_series(prices, fast)
    ema_slow = ema_series(prices, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema_series(macd_line, signal)
    return {
        "macd": round(macd_line[-1], 4),
        "signal": round(signal_line[-1], 4),
        "histogram": round(macd_line[-1] - signal_line[-1], 4),
    }


def compute_all(prices_ohlcv: list[dict]) -> dict:
    closes = [r["close_price"] for r in prices_ohlcv]
    s5  = sma(closes, 5)
    s10 = sma(closes, 10)
    s20 = sma(closes, 20)
    s60 = sma(closes, 60)
    s120 = sma(closes, 120)
    m = macd_fixed(closes)
    return {
        "sma_5":   s5,
        "sma_10":  s10,
        "sma_20":  s20,
        "sma_60":  s60,
        "sma_120": s120,
        "rsi_14":  rsi(closes),
        "macd":         m["macd"],
        "macd_signal":  m["signal"],
        "macd_histogram": m["histogram"],
        "bias_rate": round((closes[-1] - s20) / s20 * 100, 2) if s20 else None,
        "short_trend_confirmed": (
            s5 is not None and s20 is not None and s60 is not None and s5 < s20 < s60
        ),
    }

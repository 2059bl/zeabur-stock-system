"""
scoring_agent — 多維度空頭綜合評分
評分維度（總分 0~100）：
  空頭排列      25 分（5MA<20MA<60MA）
  RSI 超賣度    25 分（RSI 越低分越高）
  外資連續賣超  20 分（每連賣 1 日 +5 分，上限 20）
  券資比偏高    15 分（>10% 開始計分，>30% 滿分）
  負面新聞情緒  15 分（sentiment < 0 時加分）
"""
import logging
from datetime import date as _date

from utils.db import execute, fetch_all

logger = logging.getLogger(__name__)


def _calc_composite_score(
    is_bear_alignment: bool,
    rsi_14: float | None,
    foreign_consecutive_days: int,
    short_to_margin_ratio: float | None,
    sentiment_score: float | None,
) -> float:
    score = 0.0

    # ① 空頭排列
    if is_bear_alignment:
        score += 25

    # ② RSI 超賣度（RSI<=50 才給分；RSI=0→25分，RSI=50→0分）
    if rsi_14 is not None and rsi_14 <= 50:
        score += 25 * (50 - rsi_14) / 50

    # ③ 外資連續賣超（負值=賣超；每日 +5，上限 20）
    if foreign_consecutive_days < 0:
        score += min(abs(foreign_consecutive_days) * 5, 20)

    # ④ 券資比偏高
    if short_to_margin_ratio is not None and short_to_margin_ratio > 10:
        ratio_score = (short_to_margin_ratio - 10) / 20 * 15  # 10%→0, 30%→15
        score += min(ratio_score, 15)

    # ⑤ 負面新聞情緒（sentiment ∈[-1,0]；-1.0→15分）
    if sentiment_score is not None and sentiment_score < 0:
        score += abs(sentiment_score) * 15

    return round(min(score, 100), 2)


async def update_composite_scores(trade_date: str):
    """
    為當日所有股票計算並寫入 composite_score。
    在 news_agent、chip 更新後執行。
    """
    td = _date.fromisoformat(trade_date)
    rows = await fetch_all("""
        SELECT
            stock_code,
            sma_5, sma_20, sma_60,
            rsi_14,
            foreign_consecutive_days,
            short_to_margin_ratio,
            sentiment_score
        FROM stock_indicators
        WHERE trade_date = $1
    """, td)

    updated = 0
    for r in rows:
        is_bear = (
            r["sma_5"] is not None and r["sma_20"] is not None and r["sma_60"] is not None
            and float(r["sma_5"]) < float(r["sma_20"]) < float(r["sma_60"])
        )
        score = _calc_composite_score(
            is_bear_alignment        = is_bear,
            rsi_14                   = float(r["rsi_14"]) if r["rsi_14"] else None,
            foreign_consecutive_days = r["foreign_consecutive_days"] or 0,
            short_to_margin_ratio    = float(r["short_to_margin_ratio"]) if r["short_to_margin_ratio"] else None,
            sentiment_score          = float(r["sentiment_score"]) if r["sentiment_score"] else None,
        )
        await execute("""
            UPDATE stock_indicators SET composite_score = $3, updated_at = NOW()
            WHERE stock_code = $1 AND trade_date = $2
        """, r["stock_code"], td, score)
        updated += 1

    logger.info(f"[Scoring] 更新 {updated} 筆 composite_score")
    return updated

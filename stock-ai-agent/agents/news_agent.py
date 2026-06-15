"""
news_agent — 個股新聞抓取 + 情緒分析
來源：Yahoo Finance Search API
情緒：OpenAI GPT-4o-mini（批次評分，節省費用）
"""
import os
import logging
import httpx
from datetime import date as _date

from utils.db import execute, fetch_all

logger = logging.getLogger(__name__)

_YF_NEWS_URL = "https://query1.finance.yahoo.com/v1/finance/search?q={ticker}&newsCount=5&quotesCount=0&enableFuzzyQuery=false"
_YF_HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; StockBot/1.0)"}
_OAI_URL     = "https://api.openai.com/v1/chat/completions"
_OAI_KEY     = os.environ.get("LLM_API_KEY", "")
_OAI_MODEL   = "gpt-4o-mini"


async def _fetch_news_titles(stock_code: str) -> list[dict]:
    """從 Yahoo Finance 抓取個股最新新聞標題。"""
    ticker = f"{stock_code}.TW"
    try:
        async with httpx.AsyncClient(timeout=10, headers=_YF_HEADERS) as c:
            r = await c.get(_YF_NEWS_URL.format(ticker=ticker))
            data = r.json()
        items = data.get("news", [])
        return [
            {
                "title":  item.get("title", ""),
                "source": item.get("publisher", ""),
            }
            for item in items
            if item.get("title")
        ]
    except Exception as e:
        logger.warning(f"Yahoo 新聞抓取失敗 {stock_code}: {e}")
        return []


async def _score_sentiment(stock_code: str, titles: list[str]) -> float:
    """
    用 OpenAI 對新聞標題批次評分。
    回傳 -1.0（極負面）到 +1.0（極正面）的情緒分數。
    """
    if not _OAI_KEY or not titles:
        return 0.0

    prompt = (
        f"以下是台股 {stock_code} 的最新新聞標題，請評估整體情緒傾向。\n"
        f"標題：\n" + "\n".join(f"- {t}" for t in titles) +
        "\n\n請只回傳一個 -1.0 到 +1.0 之間的浮點數（負面=負數，正面=正數，中性=0），不要加任何說明。"
    )
    payload = {
        "model": _OAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 10,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                _OAI_URL,
                json=payload,
                headers={"Authorization": f"Bearer {_OAI_KEY}"},
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"].strip()
            return max(-1.0, min(1.0, float(content)))
    except Exception as e:
        logger.warning(f"情緒評分失敗 {stock_code}: {e}")
        return 0.0


async def run_news_sentiment(stock_code: str, trade_date: str) -> float:
    """
    抓取新聞並計算情緒分數，寫入 news_cache 和 stock_indicators。
    回傳情緒分數。
    """
    news = await _fetch_news_titles(stock_code)
    if not news:
        return 0.0

    titles = [n["title"] for n in news]
    score  = await _score_sentiment(stock_code, titles)

    from datetime import date as _date
    td = _date.fromisoformat(trade_date)

    # 寫入 news_cache
    for item in news:
        try:
            await execute("""
                INSERT INTO news_cache (stock_code, news_date, title, source, sentiment)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (stock_code, news_date, title) DO NOTHING
            """, stock_code, td, item["title"][:500], item.get("source", ""), score)
        except Exception:
            pass

    # 更新 stock_indicators
    await execute("""
        UPDATE stock_indicators SET sentiment_score = $3, updated_at = NOW()
        WHERE stock_code = $1 AND trade_date = $2
    """, stock_code, td, score)

    return score


async def get_recent_news(stock_code: str, days: int = 5) -> list[dict]:
    """從快取取最近 N 天新聞，供 Telegram chatbot 使用。"""
    return await fetch_all("""
        SELECT title, source, sentiment, news_date
        FROM news_cache
        WHERE stock_code = $1 AND news_date >= CURRENT_DATE - $2::integer
        ORDER BY news_date DESC, id DESC
        LIMIT 10
    """, stock_code, days)

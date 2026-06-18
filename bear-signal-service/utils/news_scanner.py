"""
國際金融新聞情緒掃描
- 來源：Reuters / BBC Business / MarketWatch / CNBC / FT RSS（免費無需 API key）
- 分析：OpenRouter claude-haiku-4-5（低成本，約 $0.0001/次）
- 目標：偵測黑天鵝 / 灰犀牛事件，補足 D9 維度
"""
import os
import asyncio
import logging
import datetime
import httpx
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# 免費 RSS 來源（國際財金重點）
RSS_SOURCES = [
    ("Reuters Business",    "https://feeds.reuters.com/reuters/businessNews"),
    ("Reuters Markets",     "https://feeds.reuters.com/reuters/USmarketsnews"),
    ("BBC Business",        "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("MarketWatch",         "https://feeds.marketwatch.com/marketwatch/marketpulse/"),
    ("CNBC Finance",        "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("FT Markets",          "https://www.ft.com/markets?format=rss"),
]

# 黑天鵝關鍵字快速檢測（不耗 LLM token）
BLACK_SWAN_KEYWORDS = [
    "bank collapse", "bank run", "bankruptcy", "default", "sovereign debt",
    "market crash", "circuit breaker", "trading halt", "flash crash",
    "nuclear", "war escalation", "invasion", "sanctions", "tariff",
    "fed emergency", "rate shock", "liquidity crisis", "credit crunch",
    "pandemic", "outbreak", "lockdown", "supply chain collapse",
    "currency crisis", "devaluation", "peg break",
    "terrorist", "assassination", "coup", "revolution",
    "chip ban", "export control", "tech decoupling",
]

GRAY_RHINO_KEYWORDS = [
    "recession", "stagflation", "inflation", "rate hike", "rate cut",
    "slowdown", "contraction", "unemployment", "layoffs",
    "trade war", "trade deficit", "debt ceiling",
    "housing bubble", "real estate crisis",
    "china slowdown", "europe crisis", "emerging market",
    "dollar strength", "yen carry trade", "yield curve",
    "ai regulation", "antitrust",
]


async def _fetch_rss(session: httpx.AsyncClient, name: str, url: str) -> list[dict]:
    """抓取單一 RSS feed，解析最近 24 小時文章。"""
    try:
        r = await session.get(url, timeout=10, follow_redirects=True)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        items = root.findall(".//item")
        articles = []
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
        for item in items[:20]:
            title   = (item.findtext("title") or "").strip()
            desc    = (item.findtext("description") or "").strip()
            pub_str = item.findtext("pubDate") or ""
            try:
                from email.utils import parsedate_to_datetime
                pub_dt = parsedate_to_datetime(pub_str)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=datetime.timezone.utc)
                if pub_dt < cutoff:
                    continue
            except Exception:
                pass
            articles.append({"source": name, "title": title, "desc": desc[:200]})
        return articles
    except Exception as e:
        logger.warning(f"RSS {name} failed: {e}")
        return []


def _quick_scan(articles: list[dict]) -> dict:
    """不用 LLM，直接關鍵字掃描，回傳觸發清單。"""
    black_hits, gray_hits = [], []
    for a in articles:
        text = (a["title"] + " " + a["desc"]).lower()
        for kw in BLACK_SWAN_KEYWORDS:
            if kw in text:
                black_hits.append(f"[{a['source']}] {a['title'][:80]}")
                break
        for kw in GRAY_RHINO_KEYWORDS:
            if kw in text:
                gray_hits.append(f"[{a['source']}] {a['title'][:80]}")
                break
    return {
        "black_swan_hits": list(set(black_hits))[:5],
        "gray_rhino_hits": list(set(gray_hits))[:8],
    }


async def _llm_sentiment(articles: list[dict]) -> dict:
    """
    用 OpenRouter claude-haiku 分析新聞情緒。
    回傳 sentiment_score (0=極度樂觀, 100=極度悲觀) + summary。
    """
    if not OPENROUTER_API_KEY or not articles:
        return {"sentiment_score": 0, "summary": "無 LLM 分析"}

    headlines = "\n".join(f"- {a['title']}" for a in articles[:30])
    prompt = f"""你是金融風險分析師，根據以下 24 小時內的國際財金新聞標題，評估對台灣股市/全球金融市場的空頭風險。

新聞標題：
{headlines}

請回答（JSON 格式）：
{{
  "sentiment_score": <0-100整數，0=極度樂觀/多頭，100=極度悲觀/空頭>,
  "risk_level": "<LOW|MEDIUM|HIGH|EXTREME>",
  "key_risks": ["<最重要的3個風險點，中文>"],
  "summary": "<2句話總結當前市場情緒，中文>"
}}

只回傳 JSON，不要其他文字。"""

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "anthropic/claude-haiku-4-5",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 300,
                },
            )
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            # 清理可能的 markdown code block
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            import json
            result = json.loads(content)
            return {
                "sentiment_score": int(result.get("sentiment_score", 0)),
                "risk_level":      result.get("risk_level", "LOW"),
                "key_risks":       result.get("key_risks", []),
                "summary":         result.get("summary", ""),
            }
    except Exception as e:
        logger.warning(f"LLM sentiment failed: {e}")
        return {"sentiment_score": 0, "summary": f"LLM分析失敗: {e}"}


async def scan_news() -> dict:
    """
    主入口：抓取所有 RSS + LLM 分析。
    回傳供 D9 使用的評分字典。
    """
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; BearSignalBot/1.0)"},
        timeout=15,
    ) as session:
        tasks = [_fetch_rss(session, name, url) for name, url in RSS_SOURCES]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    articles = []
    for r in results:
        if isinstance(r, list):
            articles.extend(r)

    logger.info(f"[News] 抓取 {len(articles)} 篇文章")

    if not articles:
        return {
            "d9_score": 0,
            "article_count": 0,
            "black_swan_hits": [],
            "gray_rhino_hits": [],
            "sentiment_score": 0,
            "summary": "無法取得新聞",
        }

    quick = _quick_scan(articles)
    llm   = await _llm_sentiment(articles)

    # D9 評分邏輯：
    # - LLM 情緒分數 40%
    # - 黑天鵝命中數 × 20（上限 60）
    # - 灰犀牛命中數 × 5（上限 40）
    black_score = min(60, len(quick["black_swan_hits"]) * 20)
    gray_score  = min(40, len(quick["gray_rhino_hits"]) * 5)
    llm_score   = llm.get("sentiment_score", 0) * 0.4
    d9 = round(min(100, black_score + gray_score + llm_score), 1)

    logger.info(f"[News] D9={d9} black={len(quick['black_swan_hits'])} "
                f"gray={len(quick['gray_rhino_hits'])} llm={llm.get('sentiment_score',0)}")

    return {
        "d9_score":         d9,
        "article_count":    len(articles),
        "black_swan_hits":  quick["black_swan_hits"],
        "gray_rhino_hits":  quick["gray_rhino_hits"],
        "sentiment_score":  llm.get("sentiment_score", 0),
        "llm_risk_level":   llm.get("risk_level", "LOW"),
        "key_risks":        llm.get("key_risks", []),
        "summary":          llm.get("summary", ""),
    }

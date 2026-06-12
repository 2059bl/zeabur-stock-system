import os
import httpx
import json
import re
import logging
from utils.db import execute

logger = logging.getLogger(__name__)

LLM_API_URL = os.environ.get("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL   = os.environ.get("LLM_MODEL", "gpt-4o-mini")

_SYSTEM_PROMPT = (
    "你是一位精通台股多空策略的量化專家。"
    "請根據提供的技術指標數據進行分析，並以以下 JSON 格式輸出，不加任何額外文字：\n"
    '{"cot_reasoning":"推理過程","recommendation":"BUY|SELL|HOLD","final_score":0-100,"confidence":0.0-1.0}'
)

_FALLBACK = {"recommendation": "HOLD", "final_score": 50, "cot_reasoning": "API 異常，保持觀望", "confidence": 0.3}


async def run_cloud_decision(stock_code: str, trade_date: str, report_text: str) -> dict:
    if not LLM_API_KEY:
        logger.warning("LLM_API_KEY 未設定，跳過 AI 決策")
        return _FALLBACK

    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"請分析以下台股日報數據：\n{report_text}"},
        ],
        "temperature": 0.1,
    }

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(LLM_API_URL, json=payload, headers=headers)
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"]
                clean = re.sub(r"```json?|```", "", raw).strip()
                res = json.loads(clean)

            await execute("""
                INSERT INTO ai_reports
                    (stock_code, report_date, report_type, agent_model,
                     cot_reasoning, final_score, recommendation, confidence)
                VALUES ($1,$2,'DECISION',$3,$4,$5,$6,$7)
                ON CONFLICT (stock_code, report_date, report_type) DO NOTHING
            """, stock_code, trade_date, LLM_MODEL,
                 res.get("cot_reasoning"), res.get("final_score"),
                 res.get("recommendation"), res.get("confidence"))
            return res

        except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
            logger.warning(f"LLM 呼叫第 {attempt+1} 次失敗: {e}")
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"LLM 回應解析失敗: {e}")
            break

    return _FALLBACK

#!/usr/bin/env python3
"""
📊 7/31 台股全市場漲停股爬蟲
目標：爬取 7/31 所有 1,500+ 上市櫃個股，篩選漲停（≥10%）個股
功能：將 202 檔漲停股按上市/上櫃和產業分類

注意：本腳本會產生 1,500+ 次 API 調用
建議：在 Zeabur 部署時設置為單次任務，而非定時任務
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

try:
    import httpx
except ImportError:
    print("安裝依賴：pip install httpx")
    exit(1)

# 配置
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FM_BASE = "https://api.finmindtrade.com/api/v4/data"

# 台股常見產業分類（基於已知股票）
INDUSTRY_MAPPING = {
    # AI 伺服器/ODM
    "AI": ["光寶科", "緯創", "緯穎", "營邦", "鴻海", "台達電", "仁寶", "英業達", "群翊"],

    # 半導體/封測
    "半導體": ["聯電", "台積電", "欣銓", "日月光", "聯發科", "同欣電", "京元電", "辛耘", "川湖"],

    # 品牌/板卡
    "品牌/板卡": ["創意", "東捷", "華碩", "世芯", "均華", "佳必琪", "麗臺", "益登", "技嘉"],

    # PCB
    "PCB": ["欣興", "南電", "景碩"],

    # 工業AI/邊緣
    "工業AI": ["智邦", "新漢", "圓剛"],

    # 光通訊/CPO
    "光通訊": ["聯亞光"],

    # 存儲
    "存儲": ["群聯"],

    # 顯示面板
    "面板": ["友達", "群創", "凌巨"],

    # 散熱/電源
    "散熱/電源": ["全漢", "雙鴻", "奇鋐", "健策", "貿聯", "迎廣"],

    # 網通
    "網通": ["中磊"],

    # 其他電子
    "其他": ["零壹", "志聖", "均豪"]
}

# ══════════════════════════════════════════════════════════════════════════════════
# FinMind API 爬蟲
# ══════════════════════════════════════════════════════════════════════════════════

async def fetch_stock_list():
    """獲取所有上市櫃個股代碼列表"""
    print("📡 獲取所有上市櫃個股代碼...")

    # FinMind 的股票代碼列表（需要手動維護或從其他來源獲取）
    # 這裡使用一個固定的代碼範圍
    codes = []

    # 上市股票代碼通常是 4 位數字或特殊代碼
    # 這裡需要一個完整的代碼清單
    # 由於無法直接從 API 獲取完整清單，使用已知的常見代碼

    print("⚠️  FinMind 無法直接提供完整股票清單")
    print("需要手動維護上市櫃個股代碼清單或使用其他資料來源")

    return []

async def fetch_daily_price(code: str, date: str) -> dict:
    """查詢單支股票的日線資料"""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                FM_BASE,
                params={
                    "dataset": "TaiwanStockPrice",
                    "data_id": code,
                    "start_date": date,
                    "end_date": date,
                    "token": FINMIND_TOKEN
                }
            )
            data = r.json()
            if data.get("status") == 200 and data.get("data"):
                row = data["data"][0]
                return {
                    "code": code,
                    "name": row.get("name", ""),
                    "close": float(row.get("close", 0)),
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("max", 0)),
                    "low": float(row.get("min", 0)),
                    "volume": int(row.get("Trading_Volume", 0)),
                    "change": ((float(row.get("close", 0)) - float(row.get("open", 0))) / float(row.get("open", 1))) * 100
                }
    except Exception as e:
        print(f"❌ {code}: {e}")

    return {}

async def fetch_all_gainers(date: str = "2026-07-31"):
    """爬取指定日期的所有漲停股"""
    print(f"\n【{date} 台股漲停股爬蟲】")
    print("=" * 100)
    print()
    print("⚠️  本腳本需要完整的股票代碼清單")
    print("   目前 FinMind 無法直接提供 1,500+ 上市櫃個股清單")
    print()
    print("✅ 替代方案：")
    print("   1. 使用已有的 83 支電子類股資料進行分類")
    print("   2. 根據公開新聞手動收集 202 檔漲停股清單")
    print("   3. 使用其他資料來源（如台灣證交所、Moneydj）")
    print()
    print("🔧 後續建議：")
    print("   • 建立公司內部股票代碼清單（含上市/上櫃標記）")
    print("   • 定期更新產業分類")
    print("   • 建立自動化的漲停股篩選系統")

if __name__ == "__main__":
    asyncio.run(fetch_all_gainers())

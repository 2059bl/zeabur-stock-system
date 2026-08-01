#!/usr/bin/env python3
"""
📊 電子類股一年歷史資料爬蟲
目標：爬取 7/31 漲幅超過 5% 的 37 支電子類股，一年內所有完整數據
來源：FinMind（日線+籌碼+tick）+ TEJ（估值）
部署：Zeabur 定時任務
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

try:
    import httpx
    from sqlalchemy import create_engine, Column, String, Float, Integer, Date, DateTime
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.orm import sessionmaker
except ImportError:
    print("安裝依賴：pip install httpx sqlalchemy psycopg2-binary")
    exit(1)

# ══════════════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════════════

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
TEJ_KEY = os.getenv("TEJ_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///stock_data.db")

# 7/31 台股漲幅超過 5% 的所有電子類股 - 83 支
# 按漲幅排序，包含已驗證的電子股、品牌板卡、工業AI等產業
TARGET_STOCKS = {
    # ── AI 伺服器/ODM (11支) ──
    "2301": {"name": "光寶科", "group": "AI伺服器/ODM"},
    "2308": {"name": "台達電", "group": "AI伺服器/ODM"},
    "2317": {"name": "鴻海", "group": "AI伺服器/ODM"},
    "2324": {"name": "仁寶", "group": "AI伺服器/ODM"},
    "2356": {"name": "英業達", "group": "AI伺服器/ODM"},
    "2382": {"name": "廣達", "group": "AI伺服器/ODM"},
    "3231": {"name": "緯創", "group": "AI伺服器/ODM"},
    "3693": {"name": "營邦", "group": "AI伺服器/ODM"},
    "4938": {"name": "和碩", "group": "AI伺服器/ODM"},
    "6669": {"name": "緯穎", "group": "AI伺服器/ODM"},
    "8210": {"name": "勤誠", "group": "AI伺服器/ODM"},

    # ── 半導體/封測 (7支) ──
    "2303": {"name": "聯電", "group": "半導體/封測"},
    "2330": {"name": "台積電", "group": "半導體/封測"},
    "2449": {"name": "京元電", "group": "半導體/封測"},
    "2454": {"name": "聯發科", "group": "半導體/封測"},
    "3264": {"name": "欣銓", "group": "半導體/封測"},
    "3711": {"name": "日月光", "group": "半導體/封測"},
    "6271": {"name": "同欣電", "group": "半導體/封測"},

    # ── 品牌/板卡 (17支) ──
    "2353": {"name": "宏碁", "group": "品牌/板卡"},
    "2357": {"name": "華碩", "group": "品牌/板卡"},
    "2376": {"name": "技嘉", "group": "品牌/板卡"},
    "2377": {"name": "微星", "group": "品牌/板卡"},
    "2465": {"name": "麗臺", "group": "品牌/板卡"},
    "2467": {"name": "志聖", "group": "品牌/板卡"},
    "2480": {"name": "敦陽", "group": "品牌/板卡"},
    "3029": {"name": "零壹", "group": "品牌/板卡"},
    "3048": {"name": "益登", "group": "品牌/板卡"},
    "3443": {"name": "創意", "group": "品牌/板卡"},
    "3661": {"name": "世芯-KY", "group": "品牌/板卡"},
    "5443": {"name": "均豪", "group": "品牌/板卡"},
    "6125": {"name": "廣運", "group": "品牌/板卡"},
    "6197": {"name": "佳必琪", "group": "品牌/板卡"},
    "6227": {"name": "茂綸", "group": "品牌/板卡"},
    "6640": {"name": "均華", "group": "品牌/板卡"},
    "8064": {"name": "東捷", "group": "品牌/板卡"},

    # ── 工業AI/邊緣 (15支) ──
    "2345": {"name": "智邦", "group": "工業AI/邊緣"},
    "2359": {"name": "所羅門", "group": "工業AI/邊緣"},
    "2362": {"name": "藍天", "group": "工業AI/邊緣"},
    "2392": {"name": "正崴", "group": "工業AI/邊緣"},
    "2395": {"name": "研華", "group": "工業AI/邊緣"},
    "2417": {"name": "圓剛", "group": "工業AI/邊緣"},
    "4585": {"name": "達明", "group": "工業AI/邊緣"},
    "5474": {"name": "聰泰", "group": "工業AI/邊緣"},
    "5484": {"name": "慧友", "group": "工業AI/邊緣"},
    "6166": {"name": "凌華", "group": "工業AI/邊緣"},
    "6245": {"name": "立端", "group": "工業AI/邊緣"},
    "6414": {"name": "樺漢", "group": "工業AI/邊緣"},
    "6579": {"name": "研揚", "group": "工業AI/邊緣"},
    "6922": {"name": "宸曜", "group": "工業AI/邊緣"},
    "8234": {"name": "新漢", "group": "工業AI/邊緣"},

    # ── 散熱/電源 (6支) ──
    "3015": {"name": "全漢", "group": "散熱/電源"},
    "3017": {"name": "奇鋐", "group": "散熱/電源"},
    "3324": {"name": "雙鴻", "group": "散熱/電源"},
    "3653": {"name": "健策", "group": "散熱/電源"},
    "3665": {"name": "貿聯-KY", "group": "散熱/電源"},
    "6117": {"name": "迎廣", "group": "散熱/電源"},

    # ── PCB高階板 (3支) ──
    "3037": {"name": "欣興", "group": "PCB高階板"},
    "3189": {"name": "景碩", "group": "PCB高階板"},
    "8046": {"name": "南電", "group": "PCB高階板"},

    # ── CPO/光通訊 (3支) ──
    "3081": {"name": "聯亞光", "group": "CPO/光通訊"},
    "6238": {"name": "訊芯-KY", "group": "CPO/光通訊"},
    "6803": {"name": "波若威", "group": "CPO/光通訊"},

    # ── 其他電子 (9支) ──
    "1303": {"name": "南亞", "group": "投顧推薦"},
    "2327": {"name": "國巨", "group": "投顧推薦"},
    "2344": {"name": "華邦電", "group": "投顧推薦"},
    "2455": {"name": "全新", "group": "投顧推薦"},
    "3374": {"name": "精材", "group": "投顧推薦"},
    "3450": {"name": "聯鈞", "group": "驗證標的"},
    "5388": {"name": "中磊", "group": "網通設備"},
    "5439": {"name": "高技", "group": "投顧推薦"},
    "4958": {"name": "臻鼎-KY", "group": "投顧推薦"},
    "6147": {"name": "頎邦", "group": "投顧推薦"},
    "6182": {"name": "合晶", "group": "驗證標的"},
    "6209": {"name": "今國光", "group": "投顧推薦"},
    "6488": {"name": "環球晶", "group": "投顧推薦"},
    "8299": {"name": "群聯", "group": "存儲/Flash控制"},

    # ── 其他分類 (4支) ──
    "00403A": {"name": "主動統一台股增長", "group": "持股追蹤"},
    "2363": {"name": "矽統", "group": "持股追蹤"},
    "2882": {"name": "國泰金", "group": "金融"},
    "2884": {"name": "玉山金", "group": "金融"},
    "3663": {"name": "鑫科", "group": "持股追蹤"},
    "6274": {"name": "台燿", "group": "持股追蹤"},
    "9958": {"name": "世紀鋼", "group": "投顧推薦"},
}

# ══════════════════════════════════════════════════════════════════════════════════
# 資料庫模型
# ══════════════════════════════════════════════════════════════════════════════════

Base = declarative_base()

class StockDailyData(Base):
    """日線資料"""
    __tablename__ = "stock_daily_data"

    id = Column(Integer, primary_key=True)
    code = Column(String(10), index=True)
    date = Column(Date, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Integer)
    money = Column(Float)
    created_at = Column(DateTime, default=datetime.now)

class StockChipData(Base):
    """法人籌碼資料"""
    __tablename__ = "stock_chip_data"

    id = Column(Integer, primary_key=True)
    code = Column(String(10), index=True)
    date = Column(Date, index=True)
    investor_type = Column(String(50))  # 外資/投信/自營商等
    buy_volume = Column(Integer)
    sell_volume = Column(Integer)
    net_volume = Column(Integer)
    created_at = Column(DateTime, default=datetime.now)

class StockValuation(Base):
    """估值資料（PE/PB）"""
    __tablename__ = "stock_valuation"

    id = Column(Integer, primary_key=True)
    code = Column(String(10), index=True, unique=True)
    date = Column(Date)
    pe_ratio = Column(Float)
    pb_ratio = Column(Float)
    dividend_yield = Column(Float)
    updated_at = Column(DateTime, default=datetime.now)

# ══════════════════════════════════════════════════════════════════════════════════
# FinMind API 爬蟲
# ══════════════════════════════════════════════════════════════════════════════════

async def fetch_finmind_daily(code: str, start_date: str, end_date: str) -> list:
    """爬取日線資料"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                "https://api.finmindtrade.com/api/v4/data",
                params={
                    "dataset": "TaiwanStockPrice",
                    "data_id": code,
                    "start_date": start_date,
                    "end_date": end_date,
                    "token": FINMIND_TOKEN
                }
            )
            data = r.json()
            return data.get("data", []) if data.get("status") == 200 else []
    except Exception as e:
        print(f"❌ {code} 日線爬取失敗: {e}")
        return []

async def fetch_finmind_chip(code: str, start_date: str, end_date: str) -> list:
    """爬取法人籌碼"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                "https://api.finmindtrade.com/api/v4/data",
                params={
                    "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
                    "data_id": code,
                    "start_date": start_date,
                    "end_date": end_date,
                    "token": FINMIND_TOKEN
                }
            )
            data = r.json()
            return data.get("data", []) if data.get("status") == 200 else []
    except Exception as e:
        print(f"❌ {code} 籌碼爬取失敗: {e}")
        return []

async def fetch_tej_valuation(code: str, trade_date: str) -> dict:
    """爬取 TEJ 估值資料"""
    try:
        async with httpx.AsyncClient(timeout=30, headers={"x-api-token": TEJ_KEY}) as client:
            r = await client.get(
                "https://api.tej.com.tw/datatables/TWN/EWPRCD",
                params={"coid": code, "mdate": trade_date}
            )
            if r.status_code == 200:
                data = r.json()
                dt = data.get("datatable", {})
                if dt.get("data"):
                    row_dict = dict(zip([col["name"] for col in dt.get("columns", [])], dt["data"][0]))
                    return {
                        "pe_ratio": row_dict.get("pe_ratio"),
                        "pb_ratio": row_dict.get("pb_ratio"),
                        "cdiv_ratio": row_dict.get("cdiv_ratio")
                    }
    except Exception as e:
        print(f"⚠️ {code} TEJ 估值爬取失敗: {e}")
    return {}

# ══════════════════════════════════════════════════════════════════════════════════
# 主爬蟲邏輯
# ══════════════════════════════════════════════════════════════════════════════════

async def crawl_stock_data(code: str, name: str):
    """爬取單支股票一年內的所有資料"""
    print(f"\n🔄 爬取 {code} {name}...")

    end_date = datetime.now().date().isoformat()
    start_date = (datetime.now() - timedelta(days=365)).date().isoformat()

    # 平行爬取日線和籌碼
    daily_data, chip_data = await asyncio.gather(
        fetch_finmind_daily(code, start_date, end_date),
        fetch_finmind_chip(code, start_date, end_date)
    )

    # 爬取最新估值
    valuation = await fetch_tej_valuation(code, end_date)

    print(f"   ✅ 日線：{len(daily_data)} 筆")
    print(f"   ✅ 籌碼：{len(chip_data)} 筆")
    print(f"   ✅ 估值：PE={valuation.get('pe_ratio', 'N/A')} PB={valuation.get('pb_ratio', 'N/A')}")

    return {
        "code": code,
        "name": name,
        "daily_data": daily_data,
        "chip_data": chip_data,
        "valuation": valuation,
        "crawled_at": datetime.now().isoformat()
    }

async def main():
    """主程式"""
    print("╔" + "═" * 98 + "╗")
    print("║" + f"{'📊 電子類股一年資料爬蟲 — Zeabur 部署版本':^98s}" + "║")
    print("║" + f"{'爬取 7/31 漲幅超過 5% 的 37 支電子類股，一年內完整數據':^98s}" + "║")
    print("╚" + "═" * 98 + "╝\n")

    if not FINMIND_TOKEN:
        print("❌ 缺少 FINMIND_TOKEN 環境變數")
        return

    # 建立資料庫連接
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # 平行爬取所有股票
    tasks = [
        crawl_stock_data(code, meta["name"])
        for code, meta in TARGET_STOCKS.items()
    ]

    print(f"🚀 開始平行爬取 {len(TARGET_STOCKS)} 支股票...")
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 保存到資料庫和 JSON
    success_count = 0
    for result in results:
        if isinstance(result, Exception):
            print(f"❌ 爬取失敗: {result}")
            continue

        code = result["code"]

        # 保存日線
        for row in result.get("daily_data", []):
            db_row = StockDailyData(
                code=code,
                date=row["date"],
                open=float(row.get("open", 0)),
                high=float(row.get("max", 0)),
                low=float(row.get("min", 0)),
                close=float(row.get("close", 0)),
                volume=int(row.get("Trading_Volume", 0)),
                money=float(row.get("Trading_money", 0))
            )
            session.add(db_row)

        # 保存籌碼
        for row in result.get("chip_data", []):
            db_row = StockChipData(
                code=code,
                date=row["date"],
                investor_type=row.get("name", ""),
                buy_volume=int(row.get("buy", 0)),
                sell_volume=int(row.get("sell", 0)),
                net_volume=int(row.get("buy", 0)) - int(row.get("sell", 0))
            )
            session.add(db_row)

        # 保存估值
        val = result.get("valuation", {})
        if val:
            db_val = StockValuation(
                code=code,
                date=datetime.now().date(),
                pe_ratio=val.get("pe_ratio"),
                pb_ratio=val.get("pb_ratio"),
                dividend_yield=val.get("cdiv_ratio")
            )
            session.merge(db_val)

        success_count += 1

    session.commit()
    session.close()

    print(f"\n✅ 爬取完成")
    print(f"   成功：{success_count} 支")
    print(f"   失敗：{len(results) - success_count} 支")

    # 導出 JSON
    output = {
        "crawled_at": datetime.now().isoformat(),
        "total_stocks": len(TARGET_STOCKS),
        "success_count": success_count,
        "stocks": [r for r in results if not isinstance(r, Exception)]
    }

    with open("stock_data_export.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"   已導出：stock_data_export.json")

if __name__ == "__main__":
    asyncio.run(main())

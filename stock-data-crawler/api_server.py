#!/usr/bin/env python3
"""
🌐 電子類股資料 API 伺服器
提供 REST API 查詢爬取的股票資料
部署在 Zeabur 上供儀表板查詢
"""

import os
from datetime import datetime
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, desc
from sqlalchemy.orm import sessionmaker

try:
    from fastapi import FastAPI
    import sys
    sys.path.insert(0, '.')
    from main import StockDailyData, StockChipData, StockValuation, TARGET_STOCKS
except ImportError:
    print("安裝依賴：pip install fastapi uvicorn")
    exit(1)

# ══════════════════════════════════════════════════════════════════════════════════
# FastAPI 應用
# ══════════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="📊 電子類股資料 API",
    description="7/31 漲幅超過 5% 的 37 支電子類股一年歷史資料",
    version="1.0.0"
)

# CORS 設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 資料庫設定
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///stock_data.db")
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

# ══════════════════════════════════════════════════════════════════════════════════
# API 端點
# ══════════════════════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    """根路由"""
    return {
        "service": "📊 電子類股資料 API",
        "version": "1.0.0",
        "stocks": len(TARGET_STOCKS),
        "endpoints": {
            "/stocks": "所有股票列表",
            "/stocks/{code}/daily": "日線資料",
            "/stocks/{code}/chip": "法人籌碼",
            "/stocks/{code}/valuation": "估值資料",
            "/stats": "統計資訊"
        }
    }

@app.get("/stocks")
async def list_stocks():
    """列出所有追蹤股票"""
    return {
        "total": len(TARGET_STOCKS),
        "stocks": [
            {
                "code": code,
                "name": meta["name"],
                "group": meta["group"]
            }
            for code, meta in TARGET_STOCKS.items()
        ]
    }

@app.get("/stocks/{code}/daily")
async def get_daily_data(
    code: str,
    limit: int = Query(30, ge=1, le=365),
    offset: int = Query(0, ge=0)
):
    """取得日線資料"""
    session = Session()
    try:
        rows = session.query(StockDailyData)\
            .filter(StockDailyData.code == code)\
            .order_by(desc(StockDailyData.date))\
            .limit(limit)\
            .offset(offset)\
            .all()

        return {
            "code": code,
            "name": TARGET_STOCKS.get(code, {}).get("name", ""),
            "count": len(rows),
            "data": [
                {
                    "date": row.date.isoformat(),
                    "open": row.open,
                    "high": row.high,
                    "low": row.low,
                    "close": row.close,
                    "volume": row.volume
                }
                for row in rows
            ]
        }
    finally:
        session.close()

@app.get("/stocks/{code}/chip")
async def get_chip_data(
    code: str,
    limit: int = Query(30, ge=1, le=365)
):
    """取得法人籌碼資料"""
    session = Session()
    try:
        rows = session.query(StockChipData)\
            .filter(StockChipData.code == code)\
            .order_by(desc(StockChipData.date))\
            .limit(limit)\
            .all()

        # 按投資人類型分類
        by_type = {}
        for row in rows:
            investor_type = row.investor_type
            if investor_type not in by_type:
                by_type[investor_type] = []
            by_type[investor_type].append({
                "date": row.date.isoformat(),
                "buy": row.buy_volume,
                "sell": row.sell_volume,
                "net": row.net_volume
            })

        return {
            "code": code,
            "name": TARGET_STOCKS.get(code, {}).get("name", ""),
            "chip_data": by_type
        }
    finally:
        session.close()

@app.get("/stocks/{code}/valuation")
async def get_valuation(code: str):
    """取得估值資料"""
    session = Session()
    try:
        row = session.query(StockValuation)\
            .filter(StockValuation.code == code)\
            .first()

        if not row:
            return {"code": code, "error": "無估值資料"}

        return {
            "code": code,
            "name": TARGET_STOCKS.get(code, {}).get("name", ""),
            "date": row.date.isoformat(),
            "pe_ratio": row.pe_ratio,
            "pb_ratio": row.pb_ratio,
            "dividend_yield": row.dividend_yield
        }
    finally:
        session.close()

@app.get("/stats")
async def get_stats():
    """統計資訊"""
    session = Session()
    try:
        daily_count = session.query(StockDailyData).count()
        chip_count = session.query(StockChipData).count()
        val_count = session.query(StockValuation).count()

        return {
            "total_daily_records": daily_count,
            "total_chip_records": chip_count,
            "total_valuation_records": val_count,
            "stocks_tracked": len(TARGET_STOCKS),
            "last_updated": datetime.now().isoformat()
        }
    finally:
        session.close()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

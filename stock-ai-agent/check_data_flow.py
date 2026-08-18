import asyncio
import os
import sys
from datetime import date

# 加入專案路徑
sys.path.append("/Users/ning/code/zeabur-stock-system/stock-ai-agent")

from utils.finmind_client import (
    fetch_institutional, 
    fetch_margin, 
    fetch_shareholding, 
    fetch_consecutive_foreign_days,
    fetch_futures_institutional
)

async def check_flow():
    stock_code = "2330"
    today = "2026-06-18"
    
    print(f"--- 🚀 即時數據流程稽核 (日期: {today}, 代號: {stock_code}) ---")
    
    try:
        print("\n[1] 三大法人籌碼 (institutional_investors):")
        inst = await fetch_institutional(stock_code, today)
        print(f"結果: {inst}")
        if inst and all(v == 0 for v in inst.values()):
            print("警告: 籌碼數據全為 0")

        print("\n[2] 融資融券 (margin):")
        margin = await fetch_margin(stock_code, today)
        print(f"結果: {margin}")

        print("\n[3] 外資持股比例 (shareholding):")
        share = await fetch_shareholding(stock_code, today)
        print(f"結果: {share}")

        print("\n[4] 外資連買/賣天數 (consecutive_days):")
        consecutive = await fetch_consecutive_foreign_days(stock_code, today)
        print(f"結果: {consecutive}")

        print("\n[5] 台指期三大法人淨部位 (futures_institutional):")
        futures = await fetch_futures_institutional(today)
        print(f"結果: {futures}")
        
    except Exception as e:
        print(f"\n[!] 稽核過程中斷: {e}")

if __name__ == "__main__":
    # 需要設定環境變數 FINMIND_API_KEY
    if not os.getenv("FINMIND_API_KEY"):
        os.environ["FINMIND_API_KEY"] = "YOUR_TOKEN" # 確保環境中有 Token
    
    asyncio.run(check_flow())

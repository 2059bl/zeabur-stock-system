import pandas as pd
import asyncio
from utils.db import get_pool

async def check_consistency():
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 查詢關鍵表以檢查欄位與資料樣本
        # 假設我們檢查 institutional_investors 與 stock_prices
        query_inst = "SELECT stock_code, trade_date FROM institutional_investors ORDER BY trade_date DESC LIMIT 5;"
        query_price = "SELECT stock_id, date FROM stock_prices ORDER BY date DESC LIMIT 5;"
        
        inst_data = await conn.fetch(query_inst)
        price_data = await conn.fetch(query_price)
        
        print("--- 籌碼資料驗證 (Institutional) ---")
        for row in inst_data:
            print(f"代號: {row['stock_code']}, 日期: {row['trade_date']}")
            
        print("\n--- 價格資料驗證 (Price) ---")
        for row in price_data:
            print(f"代號: {row['stock_id']}, 日期: {row['date']}")

if __name__ == "__main__":
    asyncio.run(check_consistency())

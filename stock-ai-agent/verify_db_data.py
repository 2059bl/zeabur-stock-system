import asyncio
import os
from utils.db import get_pool

async def verify():
    # 確保連線參數正確 (若無，我會在下方 print 提示)
    print(f"DATABASE_URL check: {os.environ.get('DATABASE_URL')}")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 查驗最近的 institutional_investors 記錄
            rows = await conn.fetch("SELECT * FROM information_schema.tables WHERE table_name = 'stock_institutional_investors'")
            if not rows:
                print("未找到資料表 'stock_institutional_investors'")
                return
            
            data = await conn.fetch("SELECT stock_code, trade_date, foreign_net_buy FROM stock_institutional_investors ORDER BY trade_date DESC LIMIT 5")
            print("--- 驗證資料庫數據 (最近 5 筆) ---")
            for r in data:
                print(f"日期: {r['trade_date']}, 代號: {r['stock_code']}, 外資: {r['foreign_net_buy']}")
        await pool.close()
    except Exception as e:
        print(f"資料庫連結失敗: {e}")

if __name__ == "__main__":
    asyncio.run(verify())

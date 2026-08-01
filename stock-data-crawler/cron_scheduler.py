#!/usr/bin/env python3
"""
⏰ 定時爬蟲調度器
每天早上 8:00 執行全量爬取
每天下午 3:30 執行增量更新（當日新資料）
"""

import asyncio
import os
from datetime import datetime, time
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from main import main as crawl_data

app = FastAPI(title="⏰ 定時爬蟲調度器")
scheduler = BackgroundScheduler()

# ══════════════════════════════════════════════════════════════════════════════════
# 定時任務
# ══════════════════════════════════════════════════════════════════════════════════

def scheduled_crawl():
    """定時爬取任務"""
    print(f"\n🕐 [{datetime.now()}] 開始定時爬取...")
    try:
        asyncio.run(crawl_data())
        print(f"✅ [{datetime.now()}] 定時爬取完成")
    except Exception as e:
        print(f"❌ [{datetime.now()}] 定時爬取失敗: {e}")

@app.on_event("startup")
async def startup_event():
    """應用啟動時初始化定時任務"""
    # 每天早上 8:00 執行全量爬取
    scheduler.add_job(
        scheduled_crawl,
        CronTrigger(hour=8, minute=0, timezone="Asia/Taipei"),
        id="daily_crawl",
        name="每日全量爬取（早上 8:00）"
    )

    # 每天下午 3:30 執行增量更新
    scheduler.add_job(
        scheduled_crawl,
        CronTrigger(hour=15, minute=30, timezone="Asia/Taipei"),
        id="afternoon_update",
        name="下午增量更新（下午 3:30）"
    )

    scheduler.start()
    print("✅ 定時任務已啟動")
    print("   • 每日早上 8:00：全量爬取")
    print("   • 每日下午 3:30：增量更新")

@app.on_event("shutdown")
async def shutdown_event():
    """應用關閉時停止定時任務"""
    scheduler.shutdown()

@app.get("/scheduler/status")
async def get_scheduler_status():
    """查看定時任務狀態"""
    return {
        "running": scheduler.running,
        "jobs": [
            {
                "id": job.id,
                "name": job.name,
                "next_run_time": str(job.next_run_time)
            }
            for job in scheduler.get_jobs()
        ]
    }

@app.post("/scheduler/trigger")
async def trigger_crawl():
    """手動觸發爬取"""
    try:
        await crawl_data()
        return {"status": "success", "message": "爬取已啟動"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

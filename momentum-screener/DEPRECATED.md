# momentum-screener — 已停用（合併至 stock-ai-agent）

**停用日期：** 2026-08-18  
**原因：** 與 stock-ai-agent 在 22:00 同時打 FinMind API，造成 Rate Limit 碰撞

**已遷移至 stock-ai-agent 的功能：**
- 動量篩選邏輯 → `agents/momentum_agent.py`（原本就有）
- 法人每日快取更新 → `agents/data_agent.py` `upsert_chip_data()`
- 籌碼翻轉警報 → 整合進 crash_agent.py
- institutional_daily 表 → migration_v4.sql 保留並補充欄位

**Zeabur 處理：** 在 Zeabur Console 將此服務 Suspend（不刪除，保留代碼備查）

**新排程時序（stock-ai-agent 統一管理）：**
- 22:00 主流程（報價+指標+籌碼）
- 22:20 動量篩選
- 22:35 回算報酬率
- 22:45 bear-signal-service 主信號
- 01:00 清理舊資料

# stock-data-crawler — 已停用（一次性腳本，非持久服務）

**停用日期：** 2026-08-18  
**原因：** 這是 7/31 崩盤後補抓歷史資料的一次性腳本，已完成任務。
部署為常駐服務無意義，且使用 SQLAlchemy（其他服務用 asyncpg，架構不一致）。

**Zeabur 處理：** 在 Zeabur Console 將此服務 Suspend

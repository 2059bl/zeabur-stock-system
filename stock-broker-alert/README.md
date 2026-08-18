# stock-broker-alert

台股暴跌日「帶血籌碼承接」分析 ＋ 關鍵券商分點自動追蹤警報系統

## 系統架構

```
FinMind / TEJ API
      ↓
stock-broker-alert (FastAPI + APScheduler)
      ↓
PostgreSQL (共用 Zeabur 金融專案 DB)
      ↓
Broker Analysis (blood_absorption.py / broker_score.py)
      ↓
Alert Engine (alert_engine.py)
      ↓
Telegram 即時警報
```

## 排程

| 時間 (台灣時間) | 任務 |
|---------------|------|
| 22:30 | 主流程：行情→法人→融資→分點→承接分析→Watchlist→Alert |
| 09:30 | 開盤前隔日沖風險提示 |
| 01:00 | 清理舊資料 |

## 環境變數設定

複製 `.env.example` 並填入：

```bash
cp .env.example .env
```

必填：
- `FINMIND_TOKEN` — FinMind 付費 API Token
- `DATABASE_URL` — PostgreSQL 連線（與現有 Zeabur 金融專案共用）
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — Telegram 推播

## FinMind 設定

- 付費方案限制：約 8000 requests/day
- 系統在達到 7000 次後停止擴張抓取
- 每日用量可查：`GET /api-quota`
- FinMind 官方文件：https://finmindtrade.com/analysis/#/data/document

## TEJ 設定

- TEJ 作為備用資料來源（PE/PB 估值）
- Token 存放：`TEJ_API_KEY` 環境變數

## PostgreSQL 設定

使用現有 Zeabur 金融專案 PostgreSQL，執行 schema：

```bash
# 先確認 migration_v4.sql 已完成
psql $DATABASE_URL < database/schema.sql
```

## 建立股票池

修改 `config/stocks.yaml` 和 `config/etfs.yaml`，重啟服務後自動 sync：

```bash
# 或手動觸發 sync
POST /watchlist/rebuild
```

## 第一次執行 — 歷史資料分析

```bash
# 1. 手動分析指定暴跌日（先確保行情資料在 DB）
POST /analyze/blood-day?trade_date=2026-07-30

# 2. 建立 Watchlist
POST /watchlist/rebuild

# 3. 查看暴跌日分析報告
GET /blood-day/report?trade_date=2026-07-30
GET /blood-day/report/markdown?trade_date=2026-07-30
```

## 手動重新分析

```bash
# 重新分析指定日期（DB 有 cache，不會重複打 API）
POST /analyze/blood-day?trade_date=2026-07-17
POST /analyze/blood-day?trade_date=2026-07-20
POST /analyze/blood-day?trade_date=2026-07-29
POST /analyze/blood-day?trade_date=2026-07-30
```

## 查看 API Quota

```bash
GET /api-quota
```

## 停止系統

在 Zeabur Console 暫停服務即可。系統有 checkpoint 機制，下次啟動從 DB cache 繼續。

## 查看警報紀錄

```bash
GET /alerts?days=7
GET /alerts?days=30&broker_code=1590
```

## EPS 分析（三率三升）

```bash
# 分析特定股票
GET /eps-analysis?codes=2330,2303,2408&fmt=md

# 輸出 Markdown 表格
GET /eps-analysis?fmt=md
```

## API 端點總覽

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | /health | 健康檢查 + API 用量 |
| POST | /analyze/blood-day | 手動觸發暴跌分析 |
| GET | /blood-day/report | 查詢分析結果（JSON）|
| GET | /blood-day/report/markdown | 查詢分析結果（Markdown 表格）|
| GET | /watchlist | Broker Watchlist |
| POST | /watchlist/rebuild | 重建 Watchlist |
| GET | /broker/top20 | 關鍵分點 Top 20 |
| GET | /eps-analysis | EPS 推估分析 |
| GET | /api-quota | API 用量查詢 |
| GET | /alerts | 警報紀錄 |
| POST | /alerts/run | 手動執行今日警報掃描 |

## 重要原則

1. **資料正確性 > 資料量** — 無法取得的資料明確標記，不補猜
2. **API 安全 > 一次抓完** — 達到安全閾值自動停止
3. **Cache-first** — DB 有資料不重複打 API
4. **先 MVP 驗證，再擴大** — 先四個暴跌日，確認後再開放全市場

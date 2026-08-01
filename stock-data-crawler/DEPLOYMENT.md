# 📦 Zeabur 部署指南 — 電子類股一年資料爬蟲

## 部署前檢查清單

### ✅ 代碼準備

- [ ] `main.py` — 核心爬蟲邏輯
- [ ] `api_server.py` — REST API 伺服器
- [ ] `cron_scheduler.py` — 定時任務調度
- [ ] `requirements.txt` — Python 依賴清單
- [ ] `Dockerfile` — Docker 部署配置
- [ ] `zeabur.json` — Zeabur 部署配置

### ✅ 本機測試

```bash
chmod +x test_local.sh
./test_local.sh
```

- [ ] Python 版本 >= 3.11
- [ ] FinMind Token 有效
- [ ] TEJ API Key 有效
- [ ] SQLite 資料庫初始化成功
- [ ] 前 3 支股票爬取成功
- [ ] API 伺服器成功啟動（http://localhost:8000）

---

## Zeabur 部署步驟

### 步驟 1：在 Zeabur 創建新專案

```bash
# 1. 訪問 Zeabur Dashboard
# https://dashboard.zeabur.com/

# 2. 點擊「新建專案」
# 3. 選擇「從 Git 部署」
# 4. 連接到你的 GitHub 倉庫
# 5. 選擇分支（main 或 deploy）
```

### 步驟 2：配置環境變數

在 Zeabur Dashboard 中設置以下環境變數：

#### 必須變數

| 變數名 | 值 | 說明 |
|--------|-----|------|
| `FINMIND_TOKEN` | `eyJ0eXAi...` | FinMind API Token |
| `TEJ_API_KEY` | `REln7q5O...` | TEJ API Key |
| `DATABASE_URL` | 見下表 | PostgreSQL 連接字串 |

#### DATABASE_URL 格式

**使用 Zeabur PostgreSQL 服務**：
```
postgresql://username:password@hostname:5432/database_name
```

**從 Zeabur 環境變數取得**：
```
${POSTGRES_CONNECTION_URL}
```

### 步驟 3：添加 PostgreSQL 資料庫

在 Zeabur 控制台：

1. **點擊「添加服務」**
2. **選擇「PostgreSQL」**
3. **選擇版本（14+）**
4. **確認創建**

自動生成的環境變數：
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_DB`
- `POSTGRES_CONNECTION_URL`（完整連接字串）

### 步驟 4：配置服務啟動命令

#### 選項 A：爬蟲服務（定時爬取）

```bash
# 啟動命令
python cron_scheduler.py

# 監聽端口
PORT=8000
```

#### 選項 B：API 服務（REST API）

```bash
# 啟動命令
python api_server.py

# 監聽端口
PORT=8000
```

#### 選項 C：雙重服務（不推薦，資源占用多）

可以創建兩個獨立的 Zeabur 服務：

1. **stock-crawler-api** — 運行 `python api_server.py`
2. **stock-crawler-cron** — 運行 `python cron_scheduler.py`

### 步驟 5：配置構建設置

#### Docker 構建

```
Dockerfile: ./stock-data-crawler/Dockerfile
Build Context: ./stock-data-crawler
```

#### 健康檢查

```
路徑: /scheduler/status
間隔: 30 秒
超時: 10 秒
失敗閾值: 3 次
```

### 步驟 6：推送代碼並部署

```bash
# 1. 提交代碼
git add stock-data-crawler/
git commit -m "feat: add stock-data-crawler for 37 electronic stocks"

# 2. 推送到 main 分支
git push origin main

# 3. Zeabur 自動部署
# 監控部署進度：https://dashboard.zeabur.com/projects/[project-id]
```

---

## 部署後驗證

### ✅ 檢查部署日誌

```bash
# 在 Zeabur Dashboard 查看部署日誌
# 預期看到：
# - "✅ 爬取完成"
# - "已儲存至資料庫"
# - "定時任務已啟動"
```

### ✅ 測試 API 端點

使用 Zeabur 提供的公網地址（例如 `https://stock-crawler.zeabur.app`）：

```bash
# 1. 檢查服務健康狀態
curl https://stock-crawler.zeabur.app/scheduler/status

# 2. 列出所有股票
curl https://stock-crawler.zeabur.app/stocks

# 3. 查詢單支股票日線
curl https://stock-crawler.zeabur.app/stocks/2301/daily

# 4. 查看統計信息
curl https://stock-crawler.zeabur.app/stats
```

### ✅ 驗證定時任務

```bash
# 手動觸發爬取（測試）
curl -X POST https://stock-crawler.zeabur.app/scheduler/trigger

# 查看是否執行成功
curl https://stock-crawler.zeabur.app/stats
```

---

## 故障排除

### 問題 1：部署失敗 — Docker 構建錯誤

**症狀**：
```
Build failed: failed to solve with frontend dockerfile.v0
```

**解決方案**：
1. 檢查 `requirements.txt` 中是否有語法錯誤
2. 確認所有依賴包名稱正確
3. 試試在本機構建測試：
   ```bash
   docker build -t stock-crawler ./stock-data-crawler
   ```

### 問題 2：運行時失敗 — API Token 無效

**症狀**：
```
❌ FinMind API: Unauthorized
```

**解決方案**：
1. 驗證環境變數設置：
   ```bash
   echo $FINMIND_TOKEN
   ```
2. 確認 Token 有效性（訪問 https://finmindtrade.com）
3. 檢查是否需要升級付費方案（API 配額）

### 問題 3：資料庫連接失敗

**症狀**：
```
sqlalchemy.exc.OperationalError: could not connect to server
```

**解決方案**：
1. 驗證 `DATABASE_URL` 環境變數：
   ```bash
   echo $DATABASE_URL
   ```
2. 檢查 PostgreSQL 服務是否正在運行
3. 確認防火牆允許連接
4. 試試從本機連接測試：
   ```bash
   psql $DATABASE_URL
   ```

### 問題 4：爬蟲超時

**症狀**：
```
asyncio.TimeoutError: Task was destroyed but it is pending!
```

**解決方案**：
1. 增加 HTTP 超時時間（在 `main.py` 中修改 `timeout=30`）
2. 減少並發數量（改為順序爬取）
3. 檢查網路連接質量

---

## 性能優化建議

### 1. 資料庫優化

```sql
-- 添加索引以加速查詢
CREATE INDEX idx_daily_code_date ON stock_daily_data(code, date DESC);
CREATE INDEX idx_chip_code_date ON stock_chip_data(code, date DESC);
CREATE INDEX idx_val_code ON stock_valuation(code);

-- 定期清理舊資料
DELETE FROM stock_daily_data WHERE date < NOW() - INTERVAL '2 years';
```

### 2. API 快取

在 `api_server.py` 中添加 Redis 快取：

```python
from fastapi_cache2 import FastAPICache2
from fastapi_cache2.backends.redis import RedisBackend

@cached(expire=3600)  # 緩存 1 小時
async def get_daily_data(code: str):
    ...
```

### 3. 爬蟲優化

減少 API 呼叫次數：

```python
# 只爬取過去 30 天的籌碼（不是全年）
end_date = datetime.now().date().isoformat()
start_date = (datetime.now() - timedelta(days=30)).date().isoformat()
```

---

## 監控和告警

### Zeabur 監控

在 Zeabur Dashboard 中設置：

1. **CPU 使用率告警** — 超過 80% 時通知
2. **記憶體使用告警** — 超過 512MB 時通知
3. **磁盤空間告警** — 超過 1GB 時通知

### 自訂監控

添加監控端點 `/health`：

```python
@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "database": check_db_connection(),
        "last_crawl": get_last_crawl_time(),
        "uptime": get_uptime()
    }
```

---

## 成本估算

### Zeabur 資源消耗

| 服務 | 配置 | 預估成本 |
|------|------|--------|
| CPU | 0.5 核 | $0.05/天 |
| 記憶體 | 512 MB | $0.02/天 |
| PostgreSQL | 5GB 存儲 | $0.5/月 |
| **月度總計** | | **~$5-10** |

### 優化建議

- 使用 Zeabur 免費額度（每月 $5）
- 共享 PostgreSQL 實例與其他服務
- 使用對象存儲替代資料庫存儲歷史資料

---

## 後續擴展

### 短期 (1-2 週)

- [ ] 整合到主儀表板（查詢 API 端點）
- [ ] 添加 Slack 通知（爬取完成/失敗)
- [ ] 實現數據導出功能 (CSV/Excel)

### 中期 (1 個月)

- [ ] 擴展至全市場 1,500+ 支股票
- [ ] 添加技術指標計算（MA、RSI 等）
- [ ] 實現歷史回測功能

### 長期 (2-3 個月)

- [ ] 機器學習預測模型
- [ ] 實時推播訊號
- [ ] 行情分析報告自動生成

---

## 聯繫與支援

- **Zeabur 文件**：https://docs.zeabur.com/
- **FinMind 文件**：https://finmindtrade.com/
- **TEJ 文件**：https://api.tej.com.tw/docs/

---

**部署日期**：2026-07-31  
**版本**：1.0.0  
**狀態**：✅ 就緒部署

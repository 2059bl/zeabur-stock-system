# 📊 電子類股一年資料爬蟲 — Zeabur 部署版本

## 項目概述

這個項目用於爬取 **7/31 台股漲幅超過 5% 的所有電子類股** — **83 支電子權值股**，一年內的完整歷史資料。

### 監控股票清單（共 83 支，按產業分類）

#### 【AI 伺服器/ODM】11 支
光寶科(2301)、台達電(2308)、鴻海(2317)、仁寶(2324)、英業達(2356)、廣達(2382)、緯創(3231)、營邦(3693)、和碩(4938)、緯穎(6669)、勤誠(8210)

#### 【半導體/封測】7 支
聯電(2303)、台積電(2330)、京元電(2449)、聯發科(2454)、欣銓(3264)、日月光(3711)、同欣電(6271)

#### 【品牌/板卡】17 支
宏碁(2353)、華碩(2357)、技嘉(2376)、微星(2377)、麗臺(2465)、志聖(2467)、敦陽(2480)、零壹(3029)、益登(3048)、創意(3443)、世芯-KY(3661)、均豪(5443)、廣運(6125)、佳必琪(6197)、茂綸(6227)、均華(6640)、東捷(8064)

#### 【工業AI/邊緣】15 支
智邦(2345)、所羅門(2359)、藍天(2362)、正崴(2392)、研華(2395)、圓剛(2417)、達明(4585)、聰泰(5474)、慧友(5484)、凌華(6166)、立端(6245)、樺漢(6414)、研揚(6579)、宸曜(6922)、新漢(8234)

#### 【散熱/電源】6 支
全漢(3015)、奇鋐(3017)、雙鴻(3324)、健策(3653)、貿聯-KY(3665)、迎廣(6117)

#### 【PCB 高階板】3 支
欣興(3037)、景碩(3189)、南電(8046)

#### 【CPO/光通訊】3 支
聯亞光(3081)、訊芯-KY(6238)、波若威(6803)

#### 【其他電子】21 支
南亞(1303)、國巨(2327)、華邦電(2344)、全新(2455)、中磊(5388)、高技(5439)、臻鼎-KY(4958)、精材(3374)、聯鈞(3450)、合晶(6182)、頎邦(6147)、今國光(6209)、環球晶(6488)、群聯(8299)、主動統一台股增長(00403A)、矽統(2363)、國泰金(2882)、玉山金(2884)、鑫科(3663)、台燿(6274)、世紀鋼(9958)

## 數據來源

### 1. **FinMind API** — 日線+籌碼+Tick
- `TaiwanStockPrice` — 日線資料（開高低收）
- `TaiwanStockInstitutionalInvestorsBuySell` — 法人籌碼
- `TaiwanStockPriceTick` — 分時成交（可選）

### 2. **TEJ API** — 估值資料
- `PE Ratio` — 本益比
- `PB Ratio` — 股淨比
- `Dividend Yield` — 現金殖利率

## 資料結構

### 資料庫架構

```sql
-- 日線資料表
CREATE TABLE stock_daily_data (
    id SERIAL PRIMARY KEY,
    code VARCHAR(10),
    date DATE,
    open FLOAT,
    high FLOAT,
    low FLOAT,
    close FLOAT,
    volume INTEGER,
    money FLOAT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 法人籌碼表
CREATE TABLE stock_chip_data (
    id SERIAL PRIMARY KEY,
    code VARCHAR(10),
    date DATE,
    investor_type VARCHAR(50),
    buy_volume INTEGER,
    sell_volume INTEGER,
    net_volume INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 估值資料表
CREATE TABLE stock_valuation (
    id SERIAL PRIMARY KEY,
    code VARCHAR(10) UNIQUE,
    date DATE,
    pe_ratio FLOAT,
    pb_ratio FLOAT,
    dividend_yield FLOAT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## 部署流程

### 方式 1：本機執行（開發用）

```bash
# 1. 安裝依賴
pip install -r requirements.txt

# 2. 設置環境變數
export FINMIND_TOKEN="your_token"
export TEJ_API_KEY="your_key"
export DATABASE_URL="sqlite:///stock_data.db"

# 3. 運行爬蟲
python main.py

# 4. 啟動 API 伺服器（另一個終端）
python api_server.py
# 訪問 http://localhost:8000
```

### 方式 2：Docker 本機執行

```bash
# 1. 建立 Docker 鏡像
docker build -t stock-crawler .

# 2. 運行容器
docker run -e FINMIND_TOKEN="token" \
           -e TEJ_API_KEY="key" \
           -e DATABASE_URL="postgresql://..." \
           stock-crawler
```

### 方式 3：Zeabur 部署（生產用）

```bash
# 1. 確保已在 Zeabur 上創建新專案
# 專案名稱：stock-data-crawler

# 2. 設置環境變數
# 在 Zeabur Dashboard 設置以下環境變數：
# - FINMIND_TOKEN
# - TEJ_API_KEY
# - DATABASE_URL (PostgreSQL)

# 3. 連接 PostgreSQL 資料庫
# 在 Zeabur 上添加 PostgreSQL 服務

# 4. 推送代碼到 Git（包含 83 支電子股更新）
git add .
git commit -m "feat: expand to 83 electronic stocks with detailed classification"
git push

# 5. Zeabur 自動部署
```

## API 端點

### 1. 獲取所有股票列表

```bash
GET /stocks
```

**回應**：
```json
{
  "total": 83,
  "stocks": [
    {"code": "2301", "name": "光寶科", "group": "AI伺服器/ODM"},
    {"code": "2303", "name": "聯電", "group": "半導體/封測"},
    {"code": "2330", "name": "台積電", "group": "半導體/封測"},
    ...（共 83 支電子股，按產業分類）
  ]
}
```

### 2. 獲取日線資料

```bash
GET /stocks/{code}/daily?limit=30&offset=0
```

**回應**：
```json
{
  "code": "2301",
  "name": "光寶科",
  "count": 30,
  "data": [
    {
      "date": "2026-07-31",
      "open": 209.0,
      "high": 209.0,
      "low": 209.0,
      "close": 209.0,
      "volume": 1000000
    }
  ]
}
```

### 3. 獲取法人籌碼

```bash
GET /stocks/{code}/chip?limit=30
```

**回應**：
```json
{
  "code": "2301",
  "name": "光寶科",
  "chip_data": {
    "外資": [
      {"date": "2026-07-31", "buy": 1000, "sell": 500, "net": 500}
    ],
    "投信": [
      {"date": "2026-07-31", "buy": 200, "sell": 100, "net": 100}
    ]
  }
}
```

### 4. 獲取估值資料

```bash
GET /stocks/{code}/valuation
```

**回應**：
```json
{
  "code": "2301",
  "name": "光寶科",
  "date": "2026-07-31",
  "pe_ratio": 30.735,
  "pb_ratio": null,
  "dividend_yield": 2.35
}
```

### 5. 統計信息

```bash
GET /stats
```

**回應**：
```json
{
  "total_daily_records": 245000,
  "total_chip_records": 125000,
  "total_valuation_records": 37,
  "stocks_tracked": 37,
  "last_updated": "2026-07-31T20:00:00"
}
```

## 定時任務

### 自動爬取時間表

| 時間 | 任務 | 說明 |
|------|------|------|
| **08:00** | 全量爬取 | 爬取所有 83 支電子股的最新資料 |
| **15:30** | 增量更新 | 爬取當日新增的法人籌碼和價格 |

### 手動觸發爬取

```bash
POST /scheduler/trigger

# 回應：
# {"status": "success", "message": "爬取已啟動"}
```

## 數據分析應用

### 1. 集中漲幅分析
```
比較 7/31 漲幅超過 5% 的 37 支電子股，
統計這一天的共同特徵（法人流向、量能等）
```

### 2. 歷史回溯分析
```
對過去一年的資料進行回測，
驗證「7/31 漲幅 > 5% 的電子股」是否具有預測性
```

### 3. 籌碼面對標
```
比較這 37 支股票的法人籌碼走勢，
找出共同的買賣信號
```

## 環境變數

### 必須配置

| 變數 | 說明 | 範例 |
|------|------|------|
| `FINMIND_TOKEN` | FinMind API Token | `eyJ0eXAi...` |
| `TEJ_API_KEY` | TEJ API Key | `REln7q5O...` |
| `DATABASE_URL` | 資料庫連接 | `postgresql://user:pass@host/db` |

### 可選配置

| 變數 | 說明 | 默認值 |
|------|------|--------|
| `PORT` | API 伺服器端口 | `8000` |
| `LOG_LEVEL` | 日誌級別 | `INFO` |

## 故障排除

### 1. API 配額超限

**問題**：`Rate limit exceeded`

**解決**：
- FinMind 付費方案升級
- 減少爬蟲頻率（改為每天 1 次）
- 使用快取減少重複請求

### 2. 資料庫連接失敗

**問題**：`Connection refused`

**解決**：
- 檢查 DATABASE_URL 是否正確
- 確認 PostgreSQL 服務正在運行
- 檢查防火牆規則

### 3. 部分股票無法爬取

**問題**：某些股票返回空資料

**解決**：
- 檢查股票代碼是否正確
- 確認股票在 FinMind 中存在
- 查看 API 回應狀態碼

## 性能指標

### 爬蟲性能

| 項目 | 數值 |
|------|------|
| 爬取 83 支股票 | ~12-15 分鐘 |
| 單支股票日線 (365 天) | ~2 秒 |
| 單支股票籌碼 (365 天) | ~3 秒 |
| 單支股票估值 | ~1 秒 |

### 資料庫規模

| 項目 | 數值 |
|------|------|
| 日線記錄 | ~30,295 筆 (83 支 × 365 天) |
| 籌碼記錄 | ~90,885 筆 (83 支 × 3 種投資人 × 365 天) |
| 估值記錄 | 83 筆 |
| **總數據量** | **~1 GB** |

## 下一步

### 短期 (立即完成)

- [x] 擴展至 83 支完整電子股
- [x] 按產業分類整理
- [ ] ✅ 推送到 Zeabur 部署
- [ ] 驗證爬蟲正常運作
- [ ] 定時任務測試

### 中期 (1-2 週)

- [ ] 補充完整 202 檔漲停股數據
- [ ] 建立儀表板集成 API 查詢
- [ ] 歷史回測模塊
- [ ] 籌碼對標分析

### 長期 (1-3 個月)

- [ ] 擴展至全市場 (1,500+ 支股票)
- [ ] 實時推播訊號
- [ ] 機器學習預測模型
- [ ] 跨市場對比分析

## 文件

- [`main.py`](main.py) — 核心爬蟲邏輯
- [`api_server.py`](api_server.py) — REST API 伺服器
- [`cron_scheduler.py`](cron_scheduler.py) — 定時任務調度
- [`requirements.txt`](requirements.txt) — Python 依賴
- [`Dockerfile`](Dockerfile) — Docker 部署配置
- [`zeabur.json`](zeabur.json) — Zeabur 部署配置

---

**完成日期**：2026-07-31  
**狀態**：✅ 就緒部署  
**維護者**：Zeabur 金融小組

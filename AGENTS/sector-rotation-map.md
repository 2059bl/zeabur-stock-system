# 台股板塊輪動地圖 — 代理人協作說明

## 系統架構（2026-06-29 更新）

本系統已從「依賴 Zeabur 遠端 API」改為「本機直抓 FinMind」架構，解決 Zeabur 未即時重新部署導致拉取失敗的問題。

```
使用者雙擊 .command
  │
  ├─ 1. python3 sector_rotation_fetch.py   ← 直接呼叫 FinMind API（~15 秒）
  │       └─ 輸出：~/sector_rotation_data.json
  │
  ├─ 2. python3 -m http.server 8765        ← 提供靜態 HTTP（解決 file:// CORS 限制）
  │
  └─ 3. open http://localhost:8765/tw-sector-rotation-map.html
              └─ 自動 fetch → sector_rotation_data.json (本機)
                 └─ fallback → momentum-screener.zeabur.app/api/sector-rotation（備援）
```

## 關鍵檔案

| 檔案 | 說明 |
|------|------|
| `~/tw-sector-rotation-map.html` | 板塊輪動視覺化（單一 HTML，含 CSS+JS） |
| `~/sector_rotation_fetch.py` | FinMind 資料抓取腳本（asyncio + httpx） |
| `~/sector_rotation_data.json` | 抓取結果快取（每次啟動更新） |
| `~/Desktop/台股板塊輪動地圖.command` | 桌面啟動器（chmod +x，雙擊即用） |
| `~/.tw_stock_token` | FinMind JWT Token（格式：TOKEN=eyJ...） |

## FinMind API 規格

- Base URL: `https://api.finmindtrade.com/api/v4/data`
- Token: 讀自 `~/.tw_stock_token` 或環境變數 `FINMIND_API_KEY`
- 使用的 Dataset:
  - `TaiwanStockInstitutionalInvestorsBuySell`（三大法人）
  - `TaiwanStockPrice`（股價）
- 法人欄位名稱（`name` 欄）— 英文版（實際回傳）:
  - `Foreign_Investor`, `Foreign_Dealer_Self`, `Investment_Trust`, `Dealer_self`, `Dealer_Hedging`
- 股價欄位: `max`（最高）, `min`（最低）, `close`, `Trading_Volume`
- 最新可用資料日期：2026-06-26

## 板塊指標計算

| 指標 | 定義 |
|------|------|
| `flow20` | 近 20 交易日各股票法人累計淨買超（百萬股） |
| `accel5` | 近 5 日平均 − 前 5 日平均（加速度，百萬股/日） |
| `change5` | 代表股近 5 日漲跌幅（%） |

象限判定（`statusOf`）:
- **主力**：flow20 > 0 且 accel5 > 0
- **輪動**：flow20 < 0 且 accel5 > 0（退潮中反轉）
- **觀望**：flow20 > 0 且 accel5 < 0（流入但放緩）
- **退潮**：flow20 < 0 且 accel5 < 0

## 已知問題與解決方式

| 問題 | 根因 | 解決 |
|------|------|------|
| Safari 按鈕無法點擊 | `backdrop-filter: blur` + `position: sticky` 的 Safari bug | 已移除 header 的 `backdrop-filter` |
| Zeabur 拉取失敗 | 新 endpoint 未觸發部署 | 改為本機直抓 FinMind，Zeabur 作備援 |
| file:// CORS 限制 | 瀏覽器安全政策阻擋 fetch | Python HTTP server 提供 HTTP 協議 |

## Zeabur 服務狀態（2026-06-29）

| 服務 | URL | 狀態 |
|------|-----|------|
| momentum-screener | momentum-screener.zeabur.app | ✅ 運作中（追蹤 54 支股票） |
| ic-screener | ic-screener.zeabur.app | ✅ 運作中 |
| stock-ai-agent | twstock-agent-1781283629.zeabur.app | ❌ 休眠/停止 |

## 給後續代理人的提示

1. 如需修改板塊定義，編輯 `sector_rotation_fetch.py` 的 `SECTORS` list 即可
2. HTML 的 `enrich(d)` 函式依賴 `sector`、`flow20`、`accel5`、`change5`、`theme`、`stocks` 欄位
3. 本地 token 路徑 `~/.tw_stock_token` — 若需更換 token，直接覆寫 `TOKEN=<new_token>` 格式
4. Zeabur 的 sector-rotation endpoint 若需修復，相關程式碼在:
   - `momentum-screener/utils/sector_rotation.py`
   - `momentum-screener/main.py`（endpoint `/api/sector-rotation`）

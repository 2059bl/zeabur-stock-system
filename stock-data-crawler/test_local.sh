#!/bin/bash

# 📋 本機測試檢查清單

echo "╔════════════════════════════════════════════════════════════════════════════════╗"
echo "║                                                                                ║"
echo "║            📊 電子類股一年資料爬蟲 — 本機測試檢查清單                             ║"
echo "║                                                                                ║"
echo "╚════════════════════════════════════════════════════════════════════════════════╝"
echo ""

# 檢查環境變數
echo "【步驟 1】檢查環境變數"
echo "════════════════════════════════════════════════════════════════════════════════"
if [ -z "$FINMIND_TOKEN" ]; then
    echo "❌ 缺少 FINMIND_TOKEN"
    echo "   執行：export FINMIND_TOKEN='your_token'"
    exit 1
else
    echo "✅ FINMIND_TOKEN 已設置"
fi

if [ -z "$TEJ_API_KEY" ]; then
    echo "❌ 缺少 TEJ_API_KEY"
    echo "   執行：export TEJ_API_KEY='your_key'"
    exit 1
else
    echo "✅ TEJ_API_KEY 已設置"
fi

# 檢查 Python
echo ""
echo "【步驟 2】檢查 Python 版本"
echo "════════════════════════════════════════════════════════════════════════════════"
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 未安裝"
    exit 1
fi
python3 --version
echo "✅ Python 3 已安裝"

# 檢查依賴
echo ""
echo "【步驟 3】安裝 Python 依賴"
echo "════════════════════════════════════════════════════════════════════════════════"
pip install -r requirements.txt
if [ $? -eq 0 ]; then
    echo "✅ 依賴安裝完成"
else
    echo "❌ 依賴安裝失敗"
    exit 1
fi

# 設置資料庫
echo ""
echo "【步驟 4】初始化資料庫"
echo "════════════════════════════════════════════════════════════════════════════════"
if [ -z "$DATABASE_URL" ]; then
    export DATABASE_URL="sqlite:///stock_data.db"
    echo "⚠️  使用默認 SQLite 資料庫：stock_data.db"
else
    echo "✅ 使用自訂資料庫：$DATABASE_URL"
fi

# 執行爬蟲
echo ""
echo "【步驟 5】執行爬蟲（測試前 3 支股票）"
echo "════════════════════════════════════════════════════════════════════════════════"
python3 - <<'EOF'
import asyncio
from main import crawl_stock_data

async def test_crawl():
    # 只測試前 3 支股票
    test_stocks = [
        ("2301", "光寶科"),
        ("3231", "緯創"),
        ("6669", "緯穎"),
    ]

    for code, name in test_stocks:
        result = await crawl_stock_data(code, name)
        if isinstance(result, Exception):
            print(f"❌ {code} 爬取失敗")
        else:
            print(f"✅ {code} 爬取成功")

asyncio.run(test_crawl())
EOF

if [ $? -eq 0 ]; then
    echo "✅ 爬蟲執行成功"
else
    echo "❌ 爬蟲執行失敗"
    exit 1
fi

# 啟動 API 伺服器
echo ""
echo "【步驟 6】啟動 API 伺服器"
echo "════════════════════════════════════════════════════════════════════════════════"
echo "執行以下命令在另一個終端啟動 API 伺服器："
echo ""
echo "  python3 api_server.py"
echo ""
echo "訪問 http://localhost:8000 檢查 API"
echo ""

echo "╔════════════════════════════════════════════════════════════════════════════════╗"
echo "║                                                                                ║"
echo "║                     ✅ 本機測試完成，已準備部署到 Zeabur                        ║"
echo "║                                                                                ║"
echo "╚════════════════════════════════════════════════════════════════════════════════╝"

#!/bin/bash
# 一鍵部署 momentum-screener 到 Zeabur
set -e

cd "$(dirname "$0")/momentum-screener"

echo "📦 推送到 GitHub..."
git add -A
git diff --cached --quiet && echo "  無新變更，跳過 commit" || git commit -m "${1:-chore: update}"
git push origin main

echo "🚀 部署到 Zeabur..."
echo "momentum-screener" | npx zeabur@latest deploy

echo "✅ 完成"

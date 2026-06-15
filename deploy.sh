#!/bin/bash
# deploy.sh — 部署 momentum-screener 到 Zeabur
# 用法：./deploy.sh "commit message"
# 綁定 GitHub 自動部署後只需 git push；否則自動補跑 CLI deploy
set -e

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
SERVICE_ID="6a30125d17cd262e1f74f7d2"
ENV_ID="69f9d659e5ed304c1d844d77"
ZEABUR_TOKEN="sk-oucn3xbk2nksflf3ktkxiljo3hlh5"
MSG="${1:-chore: update}"

cd "$REPO_ROOT/momentum-screener"

# ── 1. Git commit + push ───────────────────────────────────────────────────────
echo "📦 Git push..."
git add -A
if git diff --cached --quiet; then
  echo "  無新變更，跳過 commit"
else
  git commit -m "$MSG"
fi
git push origin main

# ── 2. 確認 Zeabur 是否已綁定 GitHub（有綁定則 push 就夠了）────────────────
COMMIT_MSG=$(curl -sf -X POST "https://api.zeabur.com/graphql" \
  -H "Authorization: Bearer $ZEABUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"{ service(_id:\\\"$SERVICE_ID\\\") { latestDeployment(environmentID:\\\"$ENV_ID\\\") { commitMessage } } }\"}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['service']['latestDeployment'].get('commitMessage','') or '')" 2>/dev/null || echo "")

if [ -n "$COMMIT_MSG" ]; then
  echo "✅ 已綁定 GitHub 自動部署，push 後 Zeabur 將自動重建"
  echo "   最新部署：$COMMIT_MSG"
else
  # ── 3. 未綁定：用 CLI 手動觸發 ────────────────────────────────────────────
  echo "🚀 未偵測到自動部署，手動觸發 Zeabur CLI..."
  echo "momentum-screener" | npx zeabur@latest deploy
fi

echo "✅ 部署完成"

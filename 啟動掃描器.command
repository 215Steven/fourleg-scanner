#!/bin/bash
# 四腿外站票掃描器：雙擊啟動
cd "$(dirname "$0")" || exit 1
# 執行記錄（出問題時可查看 launcher.log）
exec > >(tee -a launcher.log) 2>&1
echo "---- $(date) ----"
PORT=8765
URL="http://127.0.0.1:$PORT"

echo "== 四腿外站票掃描器 =="

# 已經有舊的掃描器在跑：先關掉，確保用的是最新版程式
OLD=$(lsof -ti tcp:$PORT -sTCP:LISTEN 2>/dev/null)
if [ -n "$OLD" ]; then
  echo "關閉舊的掃描器，改用最新版"
  kill $OLD 2>/dev/null
  sleep 1
fi

# 1. 找 Python 3.10 以上
PY=""
for c in python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
    PY="$(command -v "$c")"; break
  fi
done

if [ -n "$PY" ]; then
  # 2a. 用現有 Python 建立專用環境
  if [ ! -x .venv/bin/python ]; then
    echo "第一次執行：建立環境（約 1 分鐘）"
    "$PY" -m venv .venv || { echo "建立環境失敗"; read -r; exit 1; }
  fi
  if ! .venv/bin/python -c "import typing_extensions; from fast_flights.shopping import fetch_shopping_results" >/dev/null 2>&1; then
    echo "安裝 faster-flights …"
    .venv/bin/python -m pip install -q --upgrade pip
    .venv/bin/python -m pip uninstall -y -q fast-flights >/dev/null 2>&1
    .venv/bin/python -m pip install -q faster-flights typing_extensions || { echo "安裝失敗，請檢查網路"; read -r; exit 1; }
  fi
  echo "啟動中，瀏覽器會自動開啟。要結束請關閉這個視窗。"
  exec .venv/bin/python app.py
else
  # 2b. 沒有合適的 Python：用 uv 下載獨立 Python 3.12（不影響系統）
  UV="$HOME/.local/bin/uv"
  if ! command -v uv >/dev/null 2>&1 && [ ! -x "$UV" ]; then
    echo "第一次執行：安裝 uv（Python 管理工具）"
    curl -LsSf https://astral.sh/uv/install.sh | sh || { echo "安裝 uv 失敗，請檢查網路"; read -r; exit 1; }
  fi
  command -v uv >/dev/null 2>&1 && UV="$(command -v uv)"
  echo "準備 Python 3.12 與套件（第一次約 1～2 分鐘）"
  echo "啟動後瀏覽器會自動開啟。要結束請關閉這個視窗。"
  exec "$UV" run --python 3.12 --with faster-flights --with typing_extensions python app.py
fi

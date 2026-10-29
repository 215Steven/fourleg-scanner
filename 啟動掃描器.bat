@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo == 四腿外站票掃描器 ==

rem 關閉舊的掃描器（只關掉監聽 8765 埠的程式）
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8765" ^| findstr LISTENING') do taskkill /PID %%p /F >nul 2>&1

rem 1. 電腦上已有 Python 3.10 以上，就用現有的
set "PY="
where py >nul 2>&1 && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set "PY=python"
)
if defined PY goto :use_python

rem 2. 沒有 Python：用 uv 自動下載一份獨立的 Python 3.12（不影響系統）
set "UV=%USERPROFILE%\.local\bin\uv.exe"
where uv >nul 2>&1 && set "UV=uv"
if "%UV%"=="uv" goto :run_uv
if exist "%UV%" goto :run_uv
echo 第一次執行：安裝 uv（Python 管理工具），約 1 分鐘
powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if not exist "%UV%" (
  echo 安裝 uv 失敗，請檢查網路後再試一次
  pause
  exit /b 1
)

:run_uv
echo 準備 Python 3.12 與套件（第一次約 1～2 分鐘）
echo 啟動後瀏覽器會自動開啟。要結束請關閉這個視窗。
"%UV%" run --python 3.12 --with faster-flights --with typing_extensions python app.py
pause
exit /b

:use_python
if not exist ".venv\Scripts\python.exe" (
  echo 第一次執行：建立環境（約 1 分鐘）
  %PY% -m venv .venv
)
".venv\Scripts\python.exe" -c "import typing_extensions; from fast_flights.shopping import fetch_shopping_results" >nul 2>&1
if errorlevel 1 (
  echo 安裝 faster-flights …
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q faster-flights typing_extensions
)
echo 啟動中，瀏覽器會自動開啟。要結束請關閉這個視窗。
".venv\Scripts\python.exe" app.py
pause

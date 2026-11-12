@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  echo Installing... first time only
  py -3.13 -m venv .venv || (echo. & echo ERROR: Python 3.13 not found. Install it from https://www.python.org/downloads/windows/ & pause & exit /b)
  .venv\Scripts\python -m pip install -r requirements.txt || (echo. & echo ERROR: install failed. & rmdir /s /q .venv & pause & exit /b)
)
if not exist .env copy .env.example .env
echo Agents running. Keep this window open.
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause

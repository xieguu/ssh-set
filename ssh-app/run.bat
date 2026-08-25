@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install SSH dependencies.
  pause
  exit /b 1
)
python run.py


@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run install.cmd first.
    pause
    exit /b 1
)
if not exist ".env" (
    echo Missing .env configuration.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -u xauusd_fib_volume_pa_ai3.py
set "trader_exit_code=%errorlevel%"
echo Trader stopped with exit code %trader_exit_code%.
pause
exit /b %trader_exit_code%

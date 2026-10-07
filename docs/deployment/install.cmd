@echo off
setlocal
cd /d "%~dp0"
py -3.12 -c "import platform,struct; assert platform.system() == 'Windows' and struct.calcsize('P') == 8, 'Requires Windows x64 Python 3.12'"
if errorlevel 1 goto failed
if not exist ".venv\Scripts\python.exe" py -3.12 -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Installation complete. Read README.md before starting.
pause
exit /b 0
:failed
echo Installation failed. Check the error above.
pause
exit /b 1

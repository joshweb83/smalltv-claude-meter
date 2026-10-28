@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
echo Installing... please wait.
python -m pip install -q requests pillow
if errorlevel 1 goto nopy
python claude_meter.py --install
timeout /t 10 >nul
exit /b
:nopy
echo.
echo [ERROR] Python not found. Install it from python.org
echo         (check "Add python.exe to PATH") and run this again.
pause

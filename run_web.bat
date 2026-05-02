@echo off
echo Checking for pyserial...
pip show pyserial >nul 2>&1
if errorlevel 1 (
    echo Installing pyserial...
    pip install pyserial
)
echo Starting Elevate CNC Controller (Web Edition)...
echo Browser will open automatically at http://localhost:5001
python elevate_web.py
pause

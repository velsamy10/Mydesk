@echo off
cd /d "%~dp0"
echo Starting NIFTY Monitor v1 (manual Refresh only)...
echo Open http://127.0.0.1:8765/
echo Ctrl+C to stop.
python -u nifty_monitor.py --port 8765 --range 3
pause

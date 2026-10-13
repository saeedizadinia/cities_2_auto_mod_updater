@echo off
cd /d "%~dp0"
py -3 updater.py >> "%LOCALAPPDATA%\CS2ManualModUpdater\monthly-output.log" 2>&1

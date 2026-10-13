@echo off
cd /d "%~dp0"
py -3 updater.py --gui
if errorlevel 1 pause

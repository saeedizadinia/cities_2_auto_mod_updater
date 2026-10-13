@echo off
setlocal
cd /d "%~dp0"
set "PYCMD=%~dp0Monthly_Check.bat"
schtasks /Create /F /SC MONTHLY /D 1 /ST 10:00 /TN "CS2 Manual Mods Update Check" /TR "\"%PYCMD%\""
echo Monthly preview-only task configured for the 1st at 10:00 local time.
pause

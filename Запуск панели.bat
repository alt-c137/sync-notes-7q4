@echo off
chcp 65001 >nul
title ilm4 panel
cd /d "%~dp0"
echo Updating bot code from GitHub...
git pull -q
echo Updating Claude Code...
call claude update >nul 2>&1
echo Installing libraries (first time takes a minute)...
python -m pip install -q -r requirements.txt
echo Starting panel: http://localhost:8765
python panel.py
pause

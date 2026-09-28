@echo off
chcp 65001 >nul
title Claude login
echo In the window below type  /login  and press Enter, then log in with your Claude account.
echo After login type  /exit  and close this window.
call claude update >nul 2>&1
claude

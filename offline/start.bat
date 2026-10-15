@echo off
rem historian2grafana: база, Grafana и приложение. Окно не закрывать, пока работаете.
chcp 65001 >nul
title historian2grafana
cd /d "%~dp0"
"runtime\python\python.exe" launcher.py start %*
if errorlevel 1 pause

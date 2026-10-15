@echo off
rem Остановить базу и Grafana (окно приложения закройте само).
chcp 65001 >nul
cd /d "%~dp0"
"runtime\python\python.exe" launcher.py stop
pause

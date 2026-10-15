@echo off
rem Проверка связи с историком.
rem   check.bat                    — проверить сохранённые настройки подключения
rem   check.bat OKFFS800HIS01 1433 — найдётся ли сервер и открыт ли порт
chcp 65001 >nul
cd /d "%~dp0"
"runtime\python\python.exe" launcher.py check %*
pause

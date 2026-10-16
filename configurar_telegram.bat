@echo off
chcp 65001 >nul
cd /d "%~dp0"
title JARVIS - notificaciones por Telegram
if not exist .venv\Scripts\python.exe (
  echo  Primero ejecuta instalar.bat
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m bot.telegram_setup
pause

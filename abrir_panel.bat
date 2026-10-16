@echo off
chcp 65001 >nul
cd /d "%~dp0"
title JARVIS - panel
if not exist .venv\Scripts\python.exe (
  echo  Primero ejecuta instalar.bat
  pause
  exit /b 1
)
set "CEL="
if exist celular_activado.txt set "CEL=--celular"
.venv\Scripts\python.exe -m bot.main panel --mode paper %CEL%
pause

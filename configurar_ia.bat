@echo off
chcp 65001 >nul
cd /d "%~dp0"
title JARVIS - llave de la IA (Anthropic)
if not exist .venv\Scripts\python.exe (
  echo  Primero ejecuta instalar.bat
  pause
  exit /b 1
)
echo.
echo  Revisa la conexion y, si falta, te pide tu llave de Anthropic (empieza con sk-ant-).
echo  Se guarda solo en el archivo .env de esta PC. No la compartas con nadie.
echo.
.venv\Scripts\python.exe -m bot.revisar
echo.
echo  Despues cierra la ventana del bot y vuelve a abrir iniciar_demo.bat.
pause

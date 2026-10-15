@echo off
chcp 65001 >nul
cd /d "%~dp0"
title JARVIS - modo DEMO (dinero ficticio, precios reales)
if not exist .venv\Scripts\python.exe (
  echo  Primero ejecuta instalar.bat
  pause
  exit /b 1
)
echo.
echo  JARVIS en modo DEMO: precios reales de Binance, dinero ficticio.
echo  Deja esta ventana abierta. Para apagar el bot, cierrala.
echo  El panel se abre solo en el navegador en unos segundos.
echo.
start "JARVIS - panel" /min cmd /c abrir_panel.bat
:loop
.venv\Scripts\python.exe -m bot.main --mode paper
echo.
echo  [%date% %time%] El bot se detuvo. Se reinicia solo en 30 segundos...
timeout /t 30 /nobreak >nul
goto loop

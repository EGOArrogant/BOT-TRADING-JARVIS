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
.venv\Scripts\python.exe -m bot.integridad >nul 2>nul && start "JARVIS - panel" /min cmd /c abrir_panel.bat
:loop
rem Antes de cada arranque: si la copia del bot esta danada, reiniciar no sirve. Se detiene y lo explica.
.venv\Scripts\python.exe -m bot.integridad
if errorlevel 3 goto danado
.venv\Scripts\python.exe -m bot.main --mode paper
if errorlevel 10 if not errorlevel 11 (
  echo.
  echo  [%date% %time%] Actualizacion aplicada: arrancando la version nueva...
  goto loop
)
echo.
echo  [%date% %time%] El bot se detuvo por un error inesperado (por ejemplo, sin internet).
echo  Se reinicia solo en 30 segundos y retoma la demo donde iba (saldo y posiciones).
timeout /t 30 /nobreak >nul
goto loop

:danado
echo.
echo  El bot NO se va a reiniciar hasta que se arregle la copia (sigue los pasos de arriba).
pause
exit /b 3

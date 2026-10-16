@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title JARVIS - actualizar
echo.
echo  === Actualizar JARVIS a la version mas reciente de GitHub ===
echo  Tu archivo .env, tus datos (bot\data) y tus reportes (bot\reports) NO se tocan.
echo  Cierra antes la ventana del bot y la del panel.
echo.
pause
set "TMPD=%TEMP%\jarvis_actualizar"
if exist "%TMPD%" rmdir /s /q "%TMPD%"
mkdir "%TMPD%"
echo  Descargando...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Invoke-WebRequest -UseBasicParsing 'https://github.com/EGOArrogant/BOT-TRADING-JARVIS/archive/refs/heads/main.zip' -OutFile '%TMPD%\main.zip'; Expand-Archive -Force '%TMPD%\main.zip' '%TMPD%'"
if errorlevel 1 goto error
if not exist "%TMPD%\BOT-TRADING-JARVIS-main\bot\engine.py" goto error
echo  Reemplazando el codigo...
robocopy "%TMPD%\BOT-TRADING-JARVIS-main" "%~dp0." /E /XD data reports .venv /XF .env celular_activado.txt /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto error
rmdir /s /q "%TMPD%"
where uv >nul 2>nul && uv pip install --python .venv\Scripts\python.exe -e . >nul 2>nul
.venv\Scripts\python.exe -m bot.integridad
if errorlevel 1 (
  echo  Ejecuta instalar.bat y vuelve a intentarlo.
  pause
  exit /b 1
)
echo.
echo  Listo. Ahora abre iniciar_demo.bat
pause
exit /b 0

:error
echo.
echo  No se pudo descargar o copiar la actualizacion. Revisa tu internet y vuelve a intentarlo.
echo  Si se repite, toma una captura y enviasela a Claude.
pause
exit /b 1

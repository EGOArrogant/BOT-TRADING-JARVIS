@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Instalando JARVIS
echo.
echo  === Instalando JARVIS (tarda unos minutos la primera vez) ===
echo.
where uv >nul 2>nul
if errorlevel 1 (
  echo  Descargando "uv", el programa que instala Python 3.12 por ti...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
)
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul
if errorlevel 1 goto sin_uv

echo  Preparando Python 3.12 solo para el bot (no toca otros Python que tengas)...
if exist .venv rmdir /s /q .venv
uv venv --python 3.12 .venv
if errorlevel 1 goto error
echo  Instalando las piezas del bot...
uv pip install --python .venv\Scripts\python.exe -e .
if errorlevel 1 goto error
if not exist .env copy .env.example .env >nul

echo.
choice /c SN /m " Evitar que la PC se suspenda mientras esta enchufada (recomendado para el bot)"
if errorlevel 2 goto revisar
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
echo  Listo: la PC no se suspendera mientras este enchufada (la pantalla si puede apagarse).

:revisar
echo.
.venv\Scripts\python.exe -m bot.revisar
echo.
pause
exit /b 0

:sin_uv
echo.
echo  No se pudo instalar "uv". Cierra esta ventana, vuelve a abrir instalar.bat y,
echo  si se repite, toma una captura y enviasela a Claude.
pause
exit /b 1

:error
echo.
echo  Algo fallo. Toma una captura de esta ventana y enviasela a Claude.
pause
exit /b 1

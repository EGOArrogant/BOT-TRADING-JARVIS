@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
> "%STARTUP%\JARVIS.bat" echo @echo off
>> "%STARTUP%\JARVIS.bat" echo cd /d "%~dp0"
>> "%STARTUP%\JARVIS.bat" echo start "JARVIS" "%~dp0iniciar_demo.bat"
echo.
echo  Listo: JARVIS arrancara solo cada vez que entres a Windows.
echo  Para quitarlo: quitar_inicio_automatico.bat
pause

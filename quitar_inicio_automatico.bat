@echo off
set "F=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\JARVIS.bat"
if exist "%F%" (
  del "%F%"
  echo  Listo: JARVIS ya no arrancara solo al encender la PC.
) else (
  echo  JARVIS no estaba configurado para arrancar solo.
)
pause

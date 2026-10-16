@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo celular> celular_activado.txt
echo.
echo  Listo: el panel se podra ver en el celular (protegido con clave).
echo.
echo  Ahora:
echo   1. Cierra la ventana del bot y vuelve a abrir iniciar_demo.bat
echo   2. Si Windows pregunta por el acceso a la red: "Redes privadas" y "Permitir acceso".
echo   3. En el panel de la PC, seccion "Ver en tu celular": escanea el codigo QR con la camara del iPhone.
echo.
echo  Para desactivarlo: desactivar_celular.bat
pause

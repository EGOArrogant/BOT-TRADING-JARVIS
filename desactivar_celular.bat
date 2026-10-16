@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist celular_activado.txt del celular_activado.txt
if exist bot\reports\paper\panel_clave.txt del bot\reports\paper\panel_clave.txt
echo.
echo  Listo: el panel vuelve a verse solo en esta PC y la clave anterior deja de servir.
echo  Cierra la ventana del bot y vuelve a abrir iniciar_demo.bat para aplicarlo.
pause

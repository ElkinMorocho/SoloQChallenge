@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ==============================================
echo  LOS GOTISH - SOLOQ CHALLENGE
echo ==============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Primero ejecuta 1_INSTALAR.bat
    pause
    exit /b 1
)
if not exist ".env" (
    echo ERROR: Falta el archivo .env.
    echo Ejecuta primero 2_CONFIGURAR_API.bat
    pause
    exit /b 1
)

echo Iniciando servidor...
echo Pagina: http://127.0.0.1:8000
echo Para detenerlo: CTRL+C
echo.
start "" "http://127.0.0.1:8000"
".venv\Scripts\python.exe" app.py
pause

@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Primero ejecuta 1_INSTALAR.bat
    pause
    exit /b 1
)
".venv\Scripts\python.exe" configurar_api.py

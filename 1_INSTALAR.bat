@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ==============================================
echo  LOS GOTISH - INSTALACION
echo ==============================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON=py"
) else (
    where python >nul 2>nul
    if %errorlevel% neq 0 (
        echo ERROR: No se encontro Python.
        echo Instala Python 3.11 o superior y vuelve a ejecutar este archivo.
        pause
        exit /b 1
    )
    set "PYTHON=python"
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creando entorno virtual...
    %PYTHON% -m venv .venv
    if %errorlevel% neq 0 goto error
) else (
    echo [1/3] El entorno virtual ya existe.
)

echo [2/3] Actualizando pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if %errorlevel% neq 0 goto error

echo [3/3] Instalando dependencias...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if %errorlevel% neq 0 goto error

echo.
echo ==============================================
echo  INSTALACION COMPLETADA
echo ==============================================
echo.
echo Ahora ejecuta: 2_CONFIGURAR_API.bat
echo.
pause
exit /b 0

:error
echo.
echo ERROR: La instalacion no pudo completarse.
pause
exit /b 1

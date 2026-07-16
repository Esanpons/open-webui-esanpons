@echo off
REM ===========================================================================
REM  Open WebUI - ARRENCAR
REM
REM  Doble clic aqui per engegar l'aplicacio. Despres obre:
REM      http://localhost:8080
REM
REM  Aixo NO compila res i NO necessita el repo: arrenca la copia instal-lada
REM  que hi ha a app\. Per posar una versio nova del codi, ves al repo i
REM  executa .\scripts\prod-update.ps1
REM
REM  Per aturar: tanca aquesta finestra o prem Ctrl+C.
REM ===========================================================================

setlocal

set "PROD_ROOT=%~dp0"
set "APP_EXE=%PROD_ROOT%app\Scripts\open-webui.exe"

REM --- Les dades viuen a data\, FORA del paquet: reinstal-lar no les toca ---
set "DATA_DIR=%PROD_ROOT%data"
set "WEBUI_SECRET_KEY_FILE=%PROD_ROOT%data\.webui_secret_key"

REM --- Imprescindible: sense aixo el frontend no es munta i / dona 404 ------
set "FROM_INIT_PY=true"

REM --- Port ------------------------------------------------------------------
REM 8090, NO 8080: el backend de DEV (scripts\dev-start.ps1 al repo) fa servir el
REM 8080. Si tots dos compartissin port, el frontend de dev acabaria parlant amb
REM el backend de PRODUCCIO i veuries dades reals mentre desenvolupes.
set "PORT=8090"
if not "%~1"=="" set "PORT=%~1"

if not exist "%APP_EXE%" (
    echo.
    echo ERROR: no trobo l'aplicacio instal-lada a:
    echo    %APP_EXE%
    echo.
    echo Cal instal-lar-la primer. Des del repo:
    echo    .\scripts\prod-install.ps1
    echo.
    pause
    exit /b 1
)

echo.
echo  ================================================
echo    Open WebUI
echo  ================================================
echo    Dades:  %DATA_DIR%
echo    Obre:   http://localhost:%PORT%
echo.
echo    (Ctrl+C o tanca la finestra per aturar)
echo  ================================================
echo.

cd /d "%PROD_ROOT%data"
"%APP_EXE%" serve --host 0.0.0.0 --port %PORT%

echo.
echo  L'aplicacio s'ha aturat.
pause

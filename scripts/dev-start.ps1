<#
.SYNOPSIS
    Arranca Open WebUI (fork esanpons) en modo desarrollo: backend + frontend.

.DESCRIPTION
    - Backend  : FastAPI/uvicorn en el entorno conda 'open-webui' (Python 3.11) -> http://localhost:8080
    - Frontend : SvelteKit/Vite                                                 -> http://localhost:5173

    Ambos se lanzan como procesos hijo. Al pulsar Ctrl+C o cerrar esta ventana,
    el bloque finally se encarga de apagar los dos (y sus procesos nietos).

    Uso:
        ./scripts/dev-start.ps1                # arranca todo y abre el navegador
        ./scripts/dev-start.ps1 -NoBrowser     # no abre el navegador
        ./scripts/dev-start.ps1 -FrontendPort 5174

    OJO con -BackendPort: el frontend tiene el 8080 HARDCODEADO en src/lib/constants.ts
    (`${location.hostname}:8080`), asi que si lo cambias el frontend seguira buscando el
    8080 y no lo encontrara. Solo sirve para comprobar que el backend arranca suelto.

    Puertos: dev backend 8080 / dev frontend 5173. PRODUCCION usa el 8090 aparte
    (D:\open-webui-production\start.bat) para que puedan convivir los dos a la vez.
#>

[CmdletBinding()]
param(
    [int]$BackendPort  = 8080,
    [int]$FrontendPort = 5173,
    [string]$CondaEnv  = "open-webui",
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

# --- Rutas ---------------------------------------------------------------
$RepoRoot   = Split-Path -Parent $PSScriptRoot          # carpeta raiz del repo
$BackendDir = Join-Path $RepoRoot "backend"
$CondaRoot  = Join-Path $env:USERPROFILE "miniconda3"
$EnvDir     = Join-Path $CondaRoot "envs\$CondaEnv"
$Uvicorn    = Join-Path $EnvDir "Scripts\uvicorn.exe"

# --- Comprobaciones previas ---------------------------------------------
if (-not (Test-Path $Uvicorn)) {
    Write-Host "ERROR: no encuentro uvicorn en el entorno conda '$CondaEnv'." -ForegroundColor Red
    Write-Host "       Esperaba: $Uvicorn" -ForegroundColor Red
    Write-Host "       Crea el entorno con: conda create -n $CondaEnv python=3.11 -y" -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path (Join-Path $RepoRoot "node_modules"))) {
    Write-Host "AVISO: no hay node_modules. Ejecuta antes: npm install --engine-strict=false" -ForegroundColor Yellow
}

# Puertos libres. Sin esto uvicorn falla con "[Errno 10048] ... solo se permite un uso de
# cada direccion de socket", el error queda sepultado bajo los warnings de Svelte, Vite se
# va a otro puerto y acabas mirando un frontend sin backend (o peor: hablando con el de
# PRODUCCION, que usa el 8090 justamente para evitarlo).
foreach ($check in @(@{ Port = $BackendPort; Que = "backend" }, @{ Port = $FrontendPort; Que = "frontend" })) {
    $busy = Get-NetTCPConnection -LocalPort $check.Port -State Listen -ErrorAction SilentlyContinue
    if (-not $busy) { continue }

    $owner = Get-Process -Id ($busy | Select-Object -First 1 -ExpandProperty OwningProcess) -ErrorAction SilentlyContinue
    Write-Host ""
    Write-Host "ERROR: el puerto $($check.Port) ($($check.Que)) ya esta ocupado por:" -ForegroundColor Red
    Write-Host "       PID $($owner.Id)  $($owner.Path)" -ForegroundColor Red
    Write-Host ""
    if ($owner.Path -like "*open-webui-production*" -and $check.Port -ne 8090) {
        Write-Host "       Es el Open WebUI de PRODUCCION, que deberia estar en el 8090." -ForegroundColor Yellow
        Write-Host "       Si sigue tomando este puerto, revisa scripts\prod-common.ps1." -ForegroundColor Yellow
    }
    Write-Host "       Cierralo con:  taskkill /PID $($owner.Id) /T /F" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

# Clave secreta de dev (persistente entre arranques para no invalidar sesiones)
if (-not $env:WEBUI_SECRET_KEY) { $env:WEBUI_SECRET_KEY = "dev-secret-key-local" }

$backend  = $null
$frontend = $null

# Mata un proceso y toda su descendencia (uvicorn/vite lanzan hijos).
function Stop-Tree($proc) {
    if ($null -eq $proc) { return }
    try {
        if (-not $proc.HasExited) {
            taskkill /PID $proc.Id /T /F 2>$null | Out-Null
        }
    } catch { }
}

try {
    Write-Host "==> Backend  : http://localhost:$BackendPort   (conda '$CondaEnv')" -ForegroundColor Cyan
    $backend = Start-Process -FilePath $Uvicorn `
        -ArgumentList @("open_webui.main:app", "--host", "127.0.0.1", "--port", "$BackendPort") `
        -WorkingDirectory $BackendDir -PassThru -NoNewWindow

    Write-Host "==> Frontend : http://localhost:$FrontendPort  (npm run dev)" -ForegroundColor Cyan
    $npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
    if (-not $npm) { $npm = "npm" }
    $frontend = Start-Process -FilePath $npm `
        -ArgumentList @("run", "dev", "--", "--port", "$FrontendPort") `
        -WorkingDirectory $RepoRoot -PassThru -NoNewWindow

    if (-not $NoBrowser) {
        # Espera a que el frontend responda antes de abrir el navegador.
        Write-Host "==> Esperando a que el frontend responda..." -ForegroundColor DarkGray
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 1
            try {
                Invoke-WebRequest -Uri "http://localhost:$FrontendPort" -UseBasicParsing -TimeoutSec 2 | Out-Null
                Start-Process "http://localhost:$FrontendPort"
                break
            } catch { }
        }
    }

    Write-Host ""
    Write-Host "Todo en marcha. Abre:  http://localhost:$FrontendPort" -ForegroundColor Green
    Write-Host "Pulsa Ctrl+C (o cierra esta ventana) para apagarlo todo." -ForegroundColor Green
    Write-Host ""

    # Bucle de vigilancia: si cualquiera de los dos muere, salimos (y el finally limpia).
    while ($true) {
        if ($backend.HasExited)  { Write-Host "El backend se ha detenido (exit $($backend.ExitCode))." -ForegroundColor Yellow;  break }
        if ($frontend.HasExited) { Write-Host "El frontend se ha detenido (exit $($frontend.ExitCode))." -ForegroundColor Yellow; break }
        Start-Sleep -Seconds 1
    }
}
finally {
    Write-Host ""
    Write-Host "==> Apagando procesos..." -ForegroundColor Magenta
    Stop-Tree $frontend
    Stop-Tree $backend
    Write-Host "==> Listo. Todo apagado." -ForegroundColor Magenta
}

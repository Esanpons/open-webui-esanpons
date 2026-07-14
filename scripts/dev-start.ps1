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
        ./scripts/dev-start.ps1 -BackendPort 8081 -FrontendPort 5174
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

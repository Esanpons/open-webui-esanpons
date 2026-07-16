<#
.SYNOPSIS
    Arrenca Open WebUI (la copia instal-lada aqui). No compila res.

.DESCRIPTION
    Engega l'aplicacio que hi ha instal-lada a app\ i obre el navegador.
    NO necessita el repo de desenvolupament per res.

    Per posar una versio nova del codi, ves al repo i executa:
        .\scripts\prod-update.ps1

    Us:
        .\start.ps1                # arrenca al port 8090 i obre el navegador
        .\start.ps1 -Port 9000     # un altre port
        .\start.ps1 -NoBrowser     # no obre el navegador

    NOTA: el port per defecte es 8090, NO 8080. El backend de DEV (scripts\dev-start.ps1
    al repo) fa servir el 8080; si tots dos compartissin port, el frontend de dev
    acabaria parlant amb el backend de PRODUCCIO i veuries dades reals fent proves.
#>

[CmdletBinding()]
param(
    [int]$Port = 8090,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'

# --- Rutes (totes relatives a aquesta carpeta: res depen del repo) ----------
$ProdRoot = $PSScriptRoot
$AppExe   = Join-Path $ProdRoot 'app\Scripts\open-webui.exe'
$DataDir  = Join-Path $ProdRoot 'data'

if (-not (Test-Path $AppExe)) {
    Write-Host ''
    Write-Host "ERROR: no trobo l'aplicacio instal-lada a:" -ForegroundColor Red
    Write-Host "   $AppExe" -ForegroundColor Red
    Write-Host ''
    Write-Host '   Cal instal-lar-la primer. Des del repo:' -ForegroundColor Yellow
    Write-Host '      .\scripts\prod-install.ps1' -ForegroundColor Yellow
    Write-Host ''
    exit 1
}

# --- Entorn ----------------------------------------------------------------
# DATA_DIR: les dades viuen a data\, FORA del paquet. Nomes per aquesta sessio:
# com a variable d'usuari global afectaria tambe 'npm run dev' al repo.
$env:DATA_DIR = $DataDir
$env:WEBUI_SECRET_KEY_FILE = Join-Path $DataDir '.webui_secret_key'

# FROM_INIT_PY: imprescindible. Sense aixo env.py busca el frontend fora del
# paquet, main.py no el munta i l'arrel torna 404 sense cap error visible.
$env:FROM_INIT_PY = 'true'

Write-Host ''
Write-Host '  ================================================' -ForegroundColor White
Write-Host '    Open WebUI' -ForegroundColor White
Write-Host '  ================================================' -ForegroundColor White
Write-Host "    Dades:  $DataDir" -ForegroundColor DarkGray
Write-Host "    App:    $(Join-Path $ProdRoot 'app')" -ForegroundColor DarkGray
Write-Host ''
Write-Host "    Obre:   http://localhost:$Port" -ForegroundColor Cyan
Write-Host '    (Ctrl+C per aturar)' -ForegroundColor DarkGray
Write-Host '  ================================================' -ForegroundColor White
Write-Host ''

# --- Obrir el navegador quan el servidor respongui -------------------------
if (-not $NoBrowser) {
    $null = Start-Job -ScriptBlock {
        param($p)
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 1
            try {
                $r = Invoke-WebRequest "http://localhost:$p/health" -UseBasicParsing -TimeoutSec 2
                if ($r.StatusCode -eq 200) { Start-Process "http://localhost:$p"; break }
            } catch { }
        }
    } -ArgumentList $Port
}

# Executem des de data\ perque qualsevol fitxer relatiu hi acabi
Push-Location $DataDir
try {
    & $AppExe serve --host 0.0.0.0 --port $Port
}
finally {
    Pop-Location
    Get-Job | Remove-Job -Force -ErrorAction SilentlyContinue
}

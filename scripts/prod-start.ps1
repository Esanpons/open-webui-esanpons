# start.ps1 - Arrenca l'Open WebUI ja instal-lat a produccio. No compila res.
#
#   .\scripts\prod-start.ps1
#   .\scripts\prod-start.ps1 -Port 9000

param(
    [int]$Port = 0
)

. (Join-Path $PSScriptRoot 'prod-common.ps1')

if (-not (Test-Path $PROD_PY)) {
    Stop-WithError @"
No hi ha cap instal-lacio a $APP_DIR.
Executa primer:  .\scripts\prod-install.ps1
"@
}

$p = if ($Port -gt 0) { $Port } else { $DEFAULT_PORT }
Start-OpenWebUI -Port $p

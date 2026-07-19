# update.ps1 - Posa la versio actual del repo a produccio, conservant les dades.
#
#   .\scripts\prod-update.ps1              compila, copia webui.db i instal-la
#   .\scripts\prod-update.ps1 -WithDeps    a mes, actualitza les dependencies Python
#                                    (cal si has tocat pyproject.toml)
#
# Les dades de D:\open-webui-production\data NO es toquen mai.

param(
    [switch]$WithDeps   # usa'l si has afegit/canviat dependencies a pyproject.toml
)

. (Join-Path $PSScriptRoot 'prod-common.ps1')

Write-Host ''
Write-Host '================================================' -ForegroundColor White
Write-Host '  Open WebUI - ACTUALITZAR PRODUCCIO' -ForegroundColor White
Write-Host '================================================' -ForegroundColor White

# ---------------------------------------------------------------------------
# 0. Hi ha instal-lacio?
# ---------------------------------------------------------------------------

if (-not (Test-Path $PROD_PY)) {
    Stop-WithError @"
No hi ha cap instal-lacio a $APP_DIR.
Executa primer:  .\scripts\prod-install.ps1
"@
}

Test-Prerequisites

# Versio que hi ha ara instal-lada, per poder-la comparar al final
$oldVer = & $PROD_PY -c "import open_webui; print(getattr(open_webui, '__version__', '?'))" 2>&1
if ($LASTEXITCODE -ne 0) { $oldVer = '?' }
Write-Ok "versio instal-lada ara: $oldVer"

# Que hi ha al repo?
Push-Location $REPO_DIR
try {
    $commit = (git rev-parse --short HEAD 2>&1)
    $dirty = (git status --porcelain 2>&1)
    Write-Ok "repo a: $commit$(if ($dirty) { ' (amb canvis sense commitejar)' })"
}
finally { Pop-Location }

# ---------------------------------------------------------------------------
# 1. Copia de seguretat ABANS de res
# ---------------------------------------------------------------------------

Write-Step 'Copia de seguretat de la base de dades'
$backup = Backup-Database -Tag 'preupdate'

# ---------------------------------------------------------------------------
# 2. Compilar la versio nova
# ---------------------------------------------------------------------------

$wheel = Invoke-FrontendAndWheelBuild

# ---------------------------------------------------------------------------
# 3. Instal-lar
# ---------------------------------------------------------------------------
# Per defecte --no-deps: es MOLT mes rapid i les dependencies rarament canvien.
# Si has tocat pyproject.toml, passa -WithDeps.

Stop-OpenWebUI

if ($WithDeps) {
    Install-Dependencies -WheelPath $wheel
} else {
    Install-Wheel -WheelPath $wheel
    Write-Host '    (dependencies no tocades; si has canviat pyproject.toml usa -WithDeps)' -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# 4. Verificar que tot es correcte i que les dades hi son
# ---------------------------------------------------------------------------

Test-Installation
Assert-DataIntact -BackupPath $backup
Install-Launchers   # refresquem els llancadors per si han canviat

$newVer = & $PROD_PY -c "import open_webui; print(getattr(open_webui, '__version__', '?'))" 2>&1
if ($LASTEXITCODE -ne 0) { $newVer = '?' }

# ---------------------------------------------------------------------------
# Fet
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host '================================================' -ForegroundColor Green
Write-Host '  ACTUALITZACIO COMPLETADA' -ForegroundColor Green
Write-Host '================================================' -ForegroundColor Green
Write-Host ''
Write-Host "  Versio:   $oldVer  ->  $newVer"
if ($backup) {
    Write-Host "  Backup:   $backup"
}
Write-Host "  Dades:    $DATA_DIR  (intactes)"
Write-Host ''
Write-Host '  Nota: produccio s''atura abans d''instal-lar per evitar corrupcions de pip a Windows.' -ForegroundColor DarkGray
Write-Host '        No s''arrenca automaticament. Per arrencar-la manualment:' -ForegroundColor DarkGray
Write-Host "        $PROD_ROOT\start.bat" -ForegroundColor White
Write-Host ''

# install.ps1 - Instal-lacio de PRIMER COP d'Open WebUI a produccio.
#
#   .\scripts\prod-install.ps1
#
# Crea D:\open-webui-production\{app,data,backups}, hi munta un Python 3.11
# independent, compila el repo i hi instal-la el paquet.
#
# Nomes cal executar-lo una vegada. Per a les seguents versions: update.ps1

param(
    [int]$Port = 0,
    [switch]$NoStart   # instal-la pero no arrenca al final
)

. (Join-Path $PSScriptRoot 'prod-common.ps1')

Write-Host ''
Write-Host '================================================' -ForegroundColor White
Write-Host '  Open WebUI - INSTAL-LACIO DE PRIMER COP' -ForegroundColor White
Write-Host '================================================' -ForegroundColor White
Write-Host "  Desti:  $PROD_ROOT"
Write-Host "  Repo:   $REPO_DIR"

# ---------------------------------------------------------------------------
# 0. Ja existeix?
# ---------------------------------------------------------------------------

if (Test-Path $PROD_PY) {
    Write-Host ''
    Write-Warn2 "Ja hi ha una instal-lacio a $APP_DIR."
    Write-Host ''
    Write-Host '    Per actualitzar-la (conservant les dades):  .\scripts\prod-update.ps1'
    Write-Host '    Per refer-la de zero, esborra abans:        Remove-Item -Recurse -Force ' -NoNewline
    Write-Host $APP_DIR
    Write-Host "    (les dades de $DATA_DIR NO es tocarien)"
    Write-Host ''
    exit 1
}

Test-Prerequisites

# ---------------------------------------------------------------------------
# 1. Carpetes
# ---------------------------------------------------------------------------

Write-Step 'Creant l''estructura de carpetes'

foreach ($dir in @($PROD_ROOT, $DATA_DIR, $BACKUP_DIR)) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Write-Ok "creada: $dir"
    } else {
        Write-Ok "ja existeix: $dir"
    }
}

# Si ja hi havia dades d'abans, avisem-ne (no les tocarem)
$existingDb = Get-DbPath
if (Test-Path $existingDb) {
    $sizeMb = [math]::Round((Get-Item $existingDb).Length / 1MB, 1)
    Write-Warn2 "Ja hi ha una webui.db a $DATA_DIR ($sizeMb MB). NO la tocarem; l'app la reaprofitara."
}

# ---------------------------------------------------------------------------
# 2. Python de produccio, independent dels envs de dev
# ---------------------------------------------------------------------------

Write-Step "Creant l'entorn Python $PY_VERSION de produccio"
Write-Host "    a $APP_DIR (pot trigar un parell de minuts)"

& $CONDA_EXE create --prefix $APP_DIR "python=$PY_VERSION" --yes 2>&1 |
    Select-Object -Last 3 | ForEach-Object { Write-Host "      $_" }

if (-not (Test-Path $PROD_PY)) {
    Stop-WithError "conda no ha pogut crear l'entorn a $APP_DIR."
}

$v = & $PROD_PY --version
Write-Ok "$v a $APP_DIR"
Write-Ok 'independent dels teus envs de conda (open-webui, bc-trainer...)'

# ---------------------------------------------------------------------------
# 3. Eines de build
# ---------------------------------------------------------------------------

Write-Step 'Instal-lant les eines de build (build, hatchling)'
& $PROD_PY -m pip install --quiet --upgrade pip build hatchling 2>&1 |
    Select-Object -Last 2 | ForEach-Object { Write-Host "      $_" }
if ($LASTEXITCODE -ne 0) {
    Stop-WithError 'No s''han pogut instal-lar les eines de build.'
}
Write-Ok 'eines de build llestes'

# ---------------------------------------------------------------------------
# 4. Compilar
# ---------------------------------------------------------------------------

$wheel = Invoke-FrontendAndWheelBuild

# ---------------------------------------------------------------------------
# 5. Instal-lar (amb dependencies: es la primera vegada)
# ---------------------------------------------------------------------------

Install-Dependencies -WheelPath $wheel

# ---------------------------------------------------------------------------
# 6. Verificar
# ---------------------------------------------------------------------------

Test-Installation

# ---------------------------------------------------------------------------
# 6b. Llancadors a D:\open-webui-production
# ---------------------------------------------------------------------------

Install-Launchers

# ---------------------------------------------------------------------------
# 7. DATA_DIR
# ---------------------------------------------------------------------------
# NO el fixem com a variable d'usuari global: afectaria tambe el repo en mode
# dev, i 'npm run dev' acabaria escrivint a les dades de PRODUCCIO.
# Cada script el posa per sessio (Set-ProductionEnvironment), que es l'ambit just:
# nomes l'app de produccio el veu.

Write-Step 'Configurant DATA_DIR'
Write-Ok "DATA_DIR = $DATA_DIR (per sessio, nomes per a produccio)"
Write-Host '    Les dades viuen FORA del paquet: reinstal-lar mai no les tocara.' -ForegroundColor DarkGray
Write-Host '    Dev (npm run dev) segueix amb les seves dades a backend\data: no es barregen.' -ForegroundColor DarkGray

# ---------------------------------------------------------------------------
# Fet
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host '================================================' -ForegroundColor Green
Write-Host '  INSTAL-LACIO COMPLETADA' -ForegroundColor Green
Write-Host '================================================' -ForegroundColor Green
Write-Host ''
Write-Host "  App:      $APP_DIR"
Write-Host "  Dades:    $DATA_DIR"
Write-Host "  Backups:  $BACKUP_DIR"
Write-Host ''
Write-Host '  PER ARRENCAR L''APP (no cal el repo):' -ForegroundColor White
Write-Host "      doble clic a  $PROD_ROOT\start.bat" -ForegroundColor Cyan
Write-Host ''
Write-Host '  Per posar una versio nova del codi (des del repo):' -ForegroundColor White
Write-Host '      .\scripts\prod-update.ps1' -ForegroundColor Cyan
Write-Host ''

if (-not $NoStart) {
    $p = if ($Port -gt 0) { $Port } else { $DEFAULT_PORT }
    Start-OpenWebUI -Port $p
}

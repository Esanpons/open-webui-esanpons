# common.ps1 - Configuracio i funcions compartides per install.ps1 i update.ps1
# No s'executa directament; els altres scripts el carreguen amb dot-sourcing.

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# CONFIGURACIO
# ---------------------------------------------------------------------------

# Arrel de produccio
$PROD_ROOT = 'D:\open-webui-production'

# L'aplicacio: env conda + paquet instal-lat. Es reemplaca a cada update.
$APP_DIR = Join-Path $PROD_ROOT 'app'

# Les dades: webui.db, uploads, config. MAI es toca.
$DATA_DIR = Join-Path $PROD_ROOT 'data'

# Copies de seguretat de webui.db abans de cada update.
$BACKUP_DIR = Join-Path $PROD_ROOT 'backups'

# Repo de desenvolupament (arrel del projecte, un nivell amunt de scripts/)
$REPO_DIR = Split-Path -Parent $PSScriptRoot

# Conda (ruta absoluta: no depenem que estigui al PATH)
$CONDA_ROOT = Join-Path $env:USERPROFILE 'miniconda3'
$CONDA_EXE = Join-Path $CONDA_ROOT 'Scripts\conda.exe'

# Python de produccio (dins APP_DIR, independent dels teus envs de dev)
$PROD_PY = Join-Path $APP_DIR 'python.exe'
$PROD_PIP_ARGS = @('-m', 'pip')

# Executable oficial que crea el paquet en instal-lar-se (entry point 'open-webui')
$PROD_EXE = Join-Path $APP_DIR 'Scripts\open-webui.exe'

# Versio de Python per a produccio (el projecte demana >= 3.11, < 3.13)
$PY_VERSION = '3.11'

# Port per defecte.
# 8090, NO 8080: scripts\dev-start.ps1 fa servir el 8080 per al backend de dev. Si tots
# dos compartissin port, el segon a arrencar no l'agafaria i el frontend de dev acabaria
# parlant amb el backend de PRODUCCIO (dades reals a la pantalla de dev).
$DEFAULT_PORT = 8090

# ---------------------------------------------------------------------------
# SORTIDA
# ---------------------------------------------------------------------------

function Write-Step {
    param([string]$Message)
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "    OK  $Message" -ForegroundColor Green
}

function Write-Warn2 {
    param([string]$Message)
    Write-Host "    !   $Message" -ForegroundColor Yellow
}

function Write-Err {
    param([string]$Message)
    Write-Host ''
    Write-Host "ERROR: $Message" -ForegroundColor Red
}

function Stop-WithError {
    param([string]$Message)
    Write-Err $Message
    exit 1
}

# ---------------------------------------------------------------------------
# COMPROVACIONS
# ---------------------------------------------------------------------------

function Test-Prerequisites {
    Write-Step 'Comprovant prerequisits'

    if (-not (Test-Path $CONDA_EXE)) {
        Stop-WithError "No trobo conda a $CONDA_EXE. Edita `$CONDA_ROOT a scripts\prod-common.ps1."
    }
    Write-Ok "conda: $CONDA_EXE"

    if (-not (Test-Path (Join-Path $REPO_DIR 'package.json'))) {
        Stop-WithError "No trobo package.json a $REPO_DIR. L'script ha de viure a <repo>\scripts\."
    }
    Write-Ok "repo: $REPO_DIR"

    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) {
        Stop-WithError 'npm no esta al PATH. Cal per compilar el frontend.'
    }
    $nodeVer = (node --version)
    Write-Ok "node: $nodeVer"
    if ($nodeVer -match '^v(\d+)' -and [int]$Matches[1] -ge 23) {
        Write-Warn2 "Node $nodeVer es molt nou per aquest projecte; si 'npm run build' falla, prova amb Node 20/22 LTS."
    }
}

# ---------------------------------------------------------------------------
# BUILD
# ---------------------------------------------------------------------------

function Invoke-FrontendAndWheelBuild {
    Write-Step 'Compilant el frontend i generant el wheel'
    Write-Host '    (aixo triga uns minuts)'

    Push-Location $REPO_DIR
    try {
        # Netegem dist/ perque despres puguem identificar el wheel nou sense ambiguitat
        $dist = Join-Path $REPO_DIR 'dist'
        if (Test-Path $dist) {
            Remove-Item -Recurse -Force $dist
        }

        # 'python -m build' invoca hatch_build.py, que ja fa 'npm install --force' +
        # 'npm run build' pel seu compte. No cal compilar el frontend a part.
        Write-Host '    build (frontend + wheel)...'
        & $PROD_PY -m build --wheel 2>&1 | ForEach-Object { Write-Host "      $_" }
        if ($LASTEXITCODE -ne 0) {
            Stop-WithError "El build ha fallat (codi $LASTEXITCODE). Revisa la sortida de sobre."
        }

        $wheel = Get-ChildItem -Path $dist -Filter '*.whl' |
                 Sort-Object LastWriteTime -Descending |
                 Select-Object -First 1
        if (-not $wheel) {
            Stop-WithError 'El build ha acabat pero no ha generat cap .whl a dist\.'
        }

        Write-Ok "wheel: $($wheel.Name) ($([math]::Round($wheel.Length / 1MB, 1)) MB)"
        return $wheel.FullName
    }
    finally {
        Pop-Location
    }
}

# ---------------------------------------------------------------------------
# INSTAL-LACIO
# ---------------------------------------------------------------------------

function Install-Wheel {
    param([string]$WheelPath)

    Write-Step 'Instal-lant el paquet a produccio'

    & $PROD_PY @PROD_PIP_ARGS install --force-reinstall --no-deps $WheelPath 2>&1 |
        Select-Object -Last 5 | ForEach-Object { Write-Host "      $_" }
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "pip install ha fallat (codi $LASTEXITCODE)."
    }

    Write-Ok 'paquet instal-lat'
}

function Install-Dependencies {
    param([string]$WheelPath)

    Write-Step 'Instal-lant dependencies (pot trigar forca la primera vegada)'

    & $PROD_PY @PROD_PIP_ARGS install $WheelPath 2>&1 |
        Select-Object -Last 5 | ForEach-Object { Write-Host "      $_" }
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "La instal-lacio de dependencies ha fallat (codi $LASTEXITCODE)."
    }

    Write-Ok 'dependencies instal-lades'
}

# ---------------------------------------------------------------------------
# VERIFICACIO
# ---------------------------------------------------------------------------

function Test-Installation {
    Write-Step 'Verificant la instal-lacio'

    # 1. El paquet importa i NO apunta al repo (ha de ser una copia independent)
    $loc = & $PROD_PY -c "import open_webui; print(open_webui.__file__)" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Stop-WithError "El paquet instal-lat no importa:`n$loc"
    }
    Write-Ok "import: $loc"

    if ($loc -like "*$REPO_DIR*") {
        Stop-WithError @"
La instal-lacio apunta al REPO ($REPO_DIR), no a una copia independent.
Aixo vol dir que hi ha un 'pip install -e' actiu. Produccio quedaria lligada al repo.
Solucio: & '$PROD_PY' -m pip uninstall -y open-webui   i torna a executar install.ps1
"@
    }
    Write-Ok 'el codi es una copia independent (el repo pot evolucionar sense afectar produccio)'

    # 2. El frontend compilat viatja dins el paquet
    $pkgDir = Split-Path -Parent $loc
    $frontend = Join-Path $pkgDir 'frontend'
    if (-not (Test-Path (Join-Path $frontend 'index.html'))) {
        Stop-WithError "El paquet no conte el frontend compilat ($frontend). El build del frontend ha fallat?"
    }
    Write-Ok 'frontend compilat inclos dins el paquet'

    # 3. Els fitxers static (icones, manifest PWA) hi son
    $static = Join-Path $pkgDir 'static'
    $manifest = Join-Path $static 'site.webmanifest'
    if (Test-Path $manifest) {
        Write-Ok 'fitxers static + manifest PWA presents'
    } else {
        Write-Warn2 "Falta site.webmanifest a $static - la PWA podria no ser instal-lable."
    }

    # 4. L'executable oficial existeix (es el que fa servir Start-OpenWebUI)
    if (-not (Test-Path $PROD_EXE)) {
        Stop-WithError "No hi ha $PROD_EXE. La instal-lacio no ha creat l'entry point."
    }
    Write-Ok 'executable open-webui.exe present'

    # 5. En RUNTIME, el frontend s'ha de resoldre dins el paquet.
    #    Si no, main.py no el munta i l'arrel torna 404 sense cap error visible.
    Set-ProductionEnvironment
    $probe = @'
from open_webui.env import FRONTEND_BUILD_DIR
print(FRONTEND_BUILD_DIR)
print('EXISTS' if (FRONTEND_BUILD_DIR / 'index.html').exists() else 'MISSING')
'@
    $env:WEBUI_SECRET_KEY = 'probe-only'   # nomes per superar el guard d'arrencada
    $out = & $PROD_PY -c $probe 2>&1
    Remove-Item Env:\WEBUI_SECRET_KEY -ErrorAction SilentlyContinue

    if ($out -join "`n" -notmatch 'EXISTS') {
        Stop-WithError @"
El frontend NO es resol en temps d'execucio:
$($out -join "`n")
L'arrel (/) tornaria 404. Comprova que FROM_INIT_PY s'estableix a Set-ProductionEnvironment.
"@
    }
    Write-Ok 'el frontend es resol correctament en temps d''execucio'
}

# ---------------------------------------------------------------------------
# DADES
# ---------------------------------------------------------------------------

function Install-Launchers {
    # Copia start.bat i start.ps1 a D:\open-webui-production perque puguis
    # arrencar l'app des d'alli, sense dependre del repo per res.
    Write-Step 'Instal-lant els llancadors a produccio'

    $src = Join-Path $PSScriptRoot 'prod-launchers'
    if (-not (Test-Path $src)) {
        Write-Warn2 "No trobo les plantilles a $src; ometo els llancadors."
        return
    }

    foreach ($f in @('start.bat', 'start.ps1')) {
        $from = Join-Path $src $f
        if (Test-Path $from) {
            Copy-Item $from (Join-Path $PROD_ROOT $f) -Force
            Write-Ok "$f -> $PROD_ROOT"
        }
    }
    Write-Host '    Doble clic a start.bat per arrencar (no cal el repo).' -ForegroundColor DarkGray
}

function Get-DbPath {
    return (Join-Path $DATA_DIR 'webui.db')
}

function Backup-Database {
    param([string]$Tag = 'update')

    $db = Get-DbPath
    if (-not (Test-Path $db)) {
        Write-Warn2 'encara no hi ha webui.db (primera instal-lacio?), res a copiar'
        return $null
    }

    if (-not (Test-Path $BACKUP_DIR)) {
        New-Item -ItemType Directory -Force -Path $BACKUP_DIR | Out-Null
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $dest = Join-Path $BACKUP_DIR "webui-$stamp-$Tag.db"
    Copy-Item $db $dest

    $sizeMb = [math]::Round((Get-Item $dest).Length / 1MB, 1)
    Write-Ok "copia de seguretat: $dest ($sizeMb MB)"

    # Conservem nomes les 10 copies mes recents
    Get-ChildItem -Path $BACKUP_DIR -Filter 'webui-*.db' |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 10 |
        Remove-Item -Force -ErrorAction SilentlyContinue

    return $dest
}

function Assert-DataIntact {
    param([string]$BackupPath)

    $db = Get-DbPath
    if (-not $BackupPath) { return }

    if (-not (Test-Path $db)) {
        Stop-WithError @"
La base de dades ha DESAPAREGUT despres de l'actualitzacio!
Tens la copia intacta a: $BackupPath
Restaura-la amb:  Copy-Item '$BackupPath' '$db'
"@
    }
    Write-Ok 'la base de dades segueix al seu lloc'
}

# ---------------------------------------------------------------------------
# EXECUCIO
# ---------------------------------------------------------------------------

function Set-ProductionEnvironment {
    # DATA_DIR es la clau de tot: fa que les dades visquin FORA del paquet,
    # i desactiva la migracio automatica de env.py que mou/esborra directoris.
    # El posem NOMES per aquesta sessio: com a variable d'usuari global afectaria
    # tambe 'npm run dev' al repo, que escriuria a les dades de PRODUCCIO.
    $env:DATA_DIR = $DATA_DIR

    # WEBUI_SECRET_KEY: 'open-webui serve' la genera i la desa a .webui_secret_key
    # del directori de treball. La volem a data\, no on toqui executar l'script.
    $env:WEBUI_SECRET_KEY_FILE = Join-Path $DATA_DIR '.webui_secret_key'

    # FROM_INIT_PY fa que env.py busqui el frontend DINS el paquet
    # (site-packages\open_webui\frontend) i no a BASE_DIR\build, que aqui no existeix
    # i deixaria l'arrel amb un 404 silencios (main.py nomes munta el frontend si el
    # directori existeix; si no, no diu res).
    # serve() tambe la posa, pero massa tard: env.py ja s'ha llegit en importar el
    # modul. Ha d'estar a l'entorn ABANS d'arrencar el proces.
    $env:FROM_INIT_PY = 'true'
}

function Start-OpenWebUI {
    param([int]$Port = $DEFAULT_PORT)

    # Guard: el 8080 es el port del backend de DEV (scripts\dev-start.ps1). Si arrenquem
    # produccio alli, el frontend de dev li parlaria a ell i veuries dades REALS fent
    # proves (i el backend de dev ni tan sols arrencaria: el port ja estaria pres).
    if ($Port -eq 8080) {
        Write-Warn2 'El port 8080 es el del backend de DEV.'
        Write-Warn2 "Si el comparteixes, el frontend de dev parlara amb PRODUCCIO."
        Write-Warn2 "Recomanat: deixa el port per defecte ($DEFAULT_PORT)."
        Write-Host ''
    }

    # Guard: algu altre ja te el port?
    $busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        $owner = Get-Process -Id ($busy | Select-Object -First 1 -Expand OwningProcess) -ErrorAction SilentlyContinue
        Stop-WithError @"
El port $Port ja esta ocupat per: $($owner.Path)
Atura aquell proces, o arrenca en un altre port:  -Port <numero>
"@
    }

    Set-ProductionEnvironment

    Write-Step "Arrencant Open WebUI al port $Port"
    Write-Host "    dades:  $DATA_DIR" -ForegroundColor DarkGray
    Write-Host "    app:    $APP_DIR" -ForegroundColor DarkGray
    Write-Host ''
    Write-Host "    Obre:   http://localhost:$Port" -ForegroundColor White
    Write-Host '    (Ctrl+C per aturar)' -ForegroundColor DarkGray
    Write-Host ''

    # Fem servir l'executable oficial 'open-webui serve', NO uvicorn directe.
    # serve() a open_webui/__init__.py fa tres coses imprescindibles:
    #   1. FROM_INIT_PY=true -> el frontend es llegeix de dins el paquet (no del repo!)
    #   2. genera/carrega WEBUI_SECRET_KEY
    #   3. loop='none' a Windows, que db.py necessita
    # (python -m open_webui no funciona: el paquet no te __main__.py)
    # Executem des de DATA_DIR perque qualsevol fitxer relatiu hi acabi.
    Push-Location $DATA_DIR
    try {
        & $PROD_EXE serve --host 0.0.0.0 --port $Port
    }
    finally {
        Pop-Location
    }
}

# Producció (Windows)

Instal·la Open WebUI a `D:\open-webui-production` com una **còpia independent del repo**.
El repo pot evolucionar (o desaparèixer) sense afectar l'aplicació instal·lada.

## Ús diari: arrencar

Ves a `D:\open-webui-production` i **fes doble clic a `start.bat`**. Prou.
No necessita el repo, no compila res, no cal PowerShell.
Obre després **http://localhost:8090**.

```powershell
# alternativa des de PowerShell (obre el navegador sol)
D:\open-webui-production\start.ps1
D:\open-webui-production\start.ps1 -Port 9000
```

### Ports: producció i dev no poden compartir-los

| | Port | Arrenca amb |
|---|---|---|
| **Producció** (backend + frontend junts) | **8090** | `start.bat` |
| **Dev** backend | 8080 | `scripts\dev-start.ps1` |
| **Dev** frontend (Vite) | 5173 | idem |

Per això producció usa el **8090 i no el 8080**: si tots dos volguessin el 8080, el segon a
arrencar no l'agafaria — i el frontend de dev (5173), que crida el 8080, acabaria parlant
amb **el backend de producció**. Veuries dades reals mentre fas proves, sense cap avís.

Passa de debò: les dades no es barregen (són fitxers `webui.db` separats), però treballes
contra producció sense saber-ho. Amb ports diferents pots tenir-los tots dos oberts alhora
sense pensar-hi. `Start-OpenWebUI` avisa si li demanes el 8080 i s'atura si el port ja està
ocupat.

## Primer cop: configurar la instal·lació

Una instal·lació nova arrenca amb la **base de dades buida**: sense canals, sense pipes i
sense models. El codi hi és tot (inclosa la taula rodona), però la configuració són *dades*
i no viatja amb el paquet — que és precisament el que fa que actualitzar no pugui esborrar
res.

1. Vas a http://localhost:8090 → **el primer usuari que es registra esdevé admin**.
2. **Activa els canals**: Admin Panel → Settings → General → *Channels*.
   Venen desactivats per defecte (`ENABLE_CHANNELS=False`). Refresca amb Ctrl+Shift+R.
3. **Importa les pipes**: Admin Panel → Functions → Import, des d'[`integrations/`](../integrations/)
   (`claude_cli_pipe.py`, `claude_agent_pipe.py`, `codex_pipe.py`). Activa-les.

La **taula rodona viu dins d'un canal**, no a Workspace → Models. Detall complet a
[`docs/collab-workspace.md`](../docs/collab-workspace.md#installacio-nova-que-cal-activar).

## Posar una versió nova del codi

Quan hagis fet canvis al repo i els vulguis a producció, **des del repo**:

```powershell
.\scripts\prod-update.ps1
```

Compila, còpia `webui.db` per si de cas, reinstal·la i verifica. Després atura l'app
i torna a fer doble clic a `start.bat`.

## Estructura

```
D:\open-webui-production\
├── start.bat   ← doble clic per arrencar
├── start.ps1   ← igual, però obre el navegador sol
├── app\        entorn Python 3.11 + paquet    (es reemplaça a cada update)
├── data\       webui.db, uploads, config      (MAI es toca)
└── backups\    còpies de webui.db            (es conserven les 10 últimes)
```

## Els fitxers d'aquesta carpeta

| Fitxer | Què és |
|---|---|
| `prod-install.ps1` | Instal·lació de **primer cop**. Ja executat; no cal repetir-lo |
| `prod-update.ps1` | **Cada versió nova**: compila i reinstal·la conservant les dades |
| `prod-start.ps1` | Arrenca des del repo (equival a `start.bat`, per comoditat) |
| `prod-common.ps1` | Configuració i funcions compartides. **No s'executa sol** |
| `prod-launchers\` | Plantilles de `start.bat`/`start.ps1` que s'hi copien en instal·lar |

`prod-common.ps1` és on viuen les rutes (`$PROD_ROOT`, `$CONDA_ROOT`, `$DEFAULT_PORT`) i
les funcions que els altres tres comparteixen. Existeix perquè aquestes ~250 línies no
estiguin triplicades: per canviar el port o la carpeta de producció, es toca **només aquí**.

Opcions:

```powershell
.\scripts\prod-update.ps1 -NoStart      # instal·la però no arrenca
.\scripts\prod-update.ps1 -WithDeps     # cal NOMÉS si has tocat pyproject.toml
```

## Dev i producció no es barregen

| | Dades |
|---|---|
| Producció (`start.bat`) | `D:\open-webui-production\data` |
| Dev al repo (`scripts\dev-start.ps1`) | `backend\data\` |

`DATA_DIR` es posa **per sessió**, mai com a variable d'usuari global — si ho fos,
`npm run dev` escriuria a les dades de producció.

## Per què les dades estan segures

- El wheel **no conté cap `data/`** — mira `force-include` a `pyproject.toml`: només
  empaqueta el frontend compilat i el CHANGELOG. Reinstal·lar no les pot sobreescriure.
- Fixar `DATA_DIR` desactiva la migració automàtica de `backend/open_webui/env.py`, que
  quan no està fixat mou el directori de dades i esborra l'original.
- `prod-update.ps1` copia `webui.db` a `backups\` **abans** de tocar res i verifica que
  hi segueix **després**. Si no hi fos, s'atura i et diu com restaurar-la.

## Per què el repo i producció són independents

S'instal·la un **wheel**, que *copia* el codi a `app\`. Mai `pip install -e` (editable),
que l'*enllaçaria* al repo. `Test-Installation` ho comprova a cada execució i s'atura si
detecta que el paquet apunta al repo.

Verificat: el procés de producció carrega 364 mòduls, **0 des del repo**.

## Detall tècnic: `FROM_INIT_PY`

Els llançadors posen `FROM_INIT_PY=true` **abans** d'arrencar. És imprescindible:

- Sense: `env.py` busca el frontend a `app\Lib\build` → no existeix → `main.py` no el
  munta → **l'arrel torna 404 sense cap error visible** (`main.py:2617` només el munta
  `if os.path.exists(...)`).
- Amb: el troba a `site-packages\open_webui\frontend` → funciona.

`open-webui serve` també la posa, però massa tard: `env.py` ja s'ha llegit en importar
el mòdul. Ha d'estar a l'entorn abans d'engegar el procés.

## Accés al teu PC

L'app corre com un procés Python normal del teu usuari: veu tot el disc, la xarxa local,
el `PATH` i les teves credencials. Per això les pipes d'`integrations\` (Claude, Codex)
funcionen. Amb Docker caldria muntar volums i injectar credencials a mà.

Contrapartida: si exposes l'app fora del PC, qui hi entri tindrà indirectament aquest
mateix accés a través de les pipes.

## Problemes

**Refer la instal·lació de zero**

```powershell
Remove-Item -Recurse -Force D:\open-webui-production\app
.\scripts\prod-install.ps1
```

Les dades de `data\` no es toquen.

**Restaurar una còpia de seguretat**

```powershell
Copy-Item D:\open-webui-production\backups\webui-<data>-preupdate.db `
          D:\open-webui-production\data\webui.db
```

**`npm run build` falla** — Node v26 compila bé (verificat). Si algun dia peta, prova
amb Node 20/22 LTS.

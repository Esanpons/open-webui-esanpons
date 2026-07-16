# Espai de treball col·laboratiu (taula rodona d'IAs)

Funcionalitat pròpia d'aquest fork: converteix qualsevol **canal** d'Open WebUI en un espai on diverses IAs (Claude Code, Codex, models d'Ollama, APIs...) col·laboren **com a iguals** — sense agent director — sobre un objectiu i, opcionalment, sobre una **carpeta-projecte del disc** compartida. Tu escrius l'objectiu; l'equip s'organitza sol.

> Pla de disseny i registre d'execució: [`docs/plans/espai-collaboratiu.md`](plans/espai-collaboratiu.md) · Detall cronològic: `DEVLOG.md`.

## Com s'usa (2 minuts)

1. Activa **Channels** (Admin Settings → General) si no ho està.
2. Crea un canal (sidebar → Canals → **+** → tipus "Canal") i entra-hi.
3. Clica el botó flotant **🤝** (a dalt a la dreta del xat) → s'obre el panell **Taula rodona**:
   - **Agents d'aquesta taula**: tria'n 2+ del desplegable (cada canal pot tenir agents diferents).
   - **Carpeta del projecte**: navegador de carpetes o escriu la ruta a mà. Els agents hi llegiran/escriuran.
   - **Activa l'espai**.
4. Escriu l'objectiu com un missatge normal i **l'equip es posa a treballar en continu**: planifiquen junts, voten el pla, executen per torns, es revisen entre ells... i si es queden en silenci amb feina pendent, el sistema els empeny a continuar. Només s'aturen quan ELLS voten que la feina està acabada (o queden en repòs de veritat, o tu els atures).
5. El teu paper: donar l'objectiu i intervenir quan vulguis (el teu missatge entra al context al torn següent). **⏹ Atura l'equip** com a fre; **▶** per reactivar-lo sense escriure res.

Tot el panell també funciona per comandes de text: `/collab help`.

## Filosofia de treball de l'equip

Els agents no són assistents independents: són un **equip unit**. Aquests principis (inspirats en una nota personal de l'Esteve del 2019 sobre com ha de funcionar una organització sense piràmide) van injectats al context de cada agent i regeixen per sobre de tot:

1. **Cap piràmide**: ningú està per sobre de ningú; no hi ha caps ni agent director. Cadascú té una **funció** segons les seves capacitats, no un rang.
2. **Submissió mútua**: cada agent escolta, demana opinió i accepta correccions; l'acord de l'equip val més que la iniciativa individual.
3. **Primer planificar, després executar**: l'equip comença en fase **📋 planificació** — parlen l'objectiu, proposen, critiquen, es reparteixen la feina al tauler — i només quan algú proposa `PLA_ACORDAT:` i la resta ho **vota** favorablement es passa a **🔨 execució**. (Guardarail `require_planning`; `/collab phase plan|exec` per forçar la fase a mà.)
4. **Coordinació de dependències**: qui depèn de la feina d'un altre, **espera** i ho diu; qui acaba una cosa de la qual algú depenia, **ho anuncia** perquè l'altre continuï.
5. **Mentoria dels nous**: el resum incremental i el tauler fan de "mentor" — un agent que s'incorpora a mitja feina rep l'estat complet de l'espai al seu primer torn.

El cicle complet d'un objectiu: 📋 planificar → vot del pla → 🔨 executar (amb revisions creuades) → `FEINA_ACABADA:` → vot de tancament → ✅ resum final → l'espai torna a 📋 per al proper objectiu.

## Què fa el sistema per sota

- **Mà alçada (mode `handraise`)**: després de cada torn, cada agent rep una crida curta ("vols intervenir? JSON amb prioritat i motiu") i parla el de més prioritat. Mode alternatiu `roundrobin` (una passada per tots).
- **Context compartit**: cada agent veu la transcripció amb autors etiquetats (`[Esteve]`, `[Codex]`...), l'arbre de fitxers del projecte, el resum incremental i el tauler de tasques.
- **Gestió de fitxers EXTERNA als models**: eina estàndard `collab_files` (auto-registrada a Tools) amb `list_project_files` / `read_project_file` / `write_project_file` — funciona amb **qualsevol** model via tool-calling. Els agents CLI (Claude Code, Codex) tenen a més la carpeta com a `cwd`, com al terminal. Després de cada torn, el sistema detecta els canvis i publica "🗂️ X ha tocat el projecte: ...".
- **Tauler de tasques compartit**: els agents el gestionen amb `list_tasks` / `create_task` / `update_task`; tu des del panell. Es guarda al canal.
- **Historial complet consultable**: TOTA la conversa queda guardada per sempre al canal. Els agents veuen els últims N missatges (`context_messages`), i quan cal revisar-ho tot o buscar una decisió antiga — perquè ho creuen convenient o perquè l'usuari els ho demana — tenen `read_conversation(offset, limit)` i `search_conversation(query)`.
- **Consens explícit**: quan un agent creu que la feina està acabada, ho proposa (eina `propose_finish` o acabant el missatge amb `FEINA_ACABADA: <resum>` — el marcador funciona amb tots els models). La resta vota; amb majoria, la ronda es tanca amb el resum final.
- **Resum incremental**: en acabar cada ronda, un agent fa de "secretari" i actualitza el resum de l'estat de la feina (guardarail `auto_summary`), que es reinjecta a les rondes següents perquè el context no creixi infinit.
- **Estadístiques**: en acabar cada ronda es publica "📊 Ronda: X torns · Y crides curtes · temps".

## Guardarails (tots configurables per espai, en calent)

| Clau | Per defecte | Què fa (0/off = desactivat) |
|---|---|---|
| `require_planning` | on | Filosofia d'equip: fase 📋 planificació (amb vot del pla) abans de la 🔨 execució |
| `max_agent_turns` | 0 (sense límit) | Fre opcional: torns seguits abans de pausar l'equip |
| `end_on_silence` | on | Repòs 😴 quan ningú vol intervenir (després de 2 empentes del sistema si queda feina) |
| `allow_self_reply` | on | Permetre dos torns seguits del mateix agent |
| `turn_timeout` | 900 s | Temps màxim d'un torn |
| `handraise_timeout` | 180 s | Temps màxim de les crides curtes (mà alçada, vot, resum) |
| `context_messages` | 30 | Missatges recents passats com a context (canviable en qualsevol moment; per a l'historial COMPLET els agents tenen `read_conversation`/`search_conversation`) |
| `auto_summary` | off | Resum incremental en acabar cada sessió (1 crida extra) |
| `max_round_seconds` | 0 | Durada màxima d'una ronda sencera |

## Variables d'entorn

| Variable | Efecte |
|---|---|
| `ENABLE_CHANNELS=true` | Default inicial per activar Channels (després mana la config de la BD) |
| `COLLAB_ALLOWED_ROOTS` | Llista blanca d'arrels per a carpetes-projecte, separades per `;` (ex. `D:\Proyectos;C:\Temp`). Sense definir: qualsevol carpeta, però només admins |
| `COLLAB_ADMIN_ONLY=true` | Només els admins poden configurar espais, gestionar tasques i llançar/aturar rondes |

## Arquitectura i punts de contacte amb el nucli

Tot el codi nou és a:

- `backend/open_webui/collab/` — `config.py` (config+guardarails), `orchestrator.py` (rondes, mà alçada, vots, resum), `commands.py` (`/collab`), `files.py` (arbre, canvis, rutes segures), `file_tools.py` (eina auto-registrada), `tasks.py` (tauler + estat compartit), `router.py` (API `/api/v1/collab`).
- `src/lib/components/collab/CollabPanel.svelte` + `src/lib/apis/collab/index.ts` — panell i client.
- `integrations/*.py` — els 3 pipes amb suport de `cwd`/handraise (còpia sincronitzada a la taula `function` de la BD).

**Hooks al nucli (llista definitiva — cercar `collab-fork` per trobar-los):**

| Fitxer | Què fa |
|---|---|
| `backend/open_webui/routers/channels.py` (~línia 1221) | Desvia els missatges de canals col·laboratius (i les comandes `/collab`) cap a l'orquestrador |
| `backend/open_webui/main.py` (~línia 750) | Registra el router `/api/v1/collab` |
| `backend/open_webui/socket/main.py` (`_make_channel_emitter`) | Fix: l'emitter de canal ara entén tots els formats de `chat:completion` (deltas OpenAI, `output` items) — sense això els missatges dels models als canals queden buits |
| `src/lib/components/channel/Channel.svelte` | Botó 🤝 + pane del panell (mateix patró que Threads) |
| `src/lib/components/layout/Sidebar/ChannelItem.svelte` | Badge 🤝 als canals amb espai actiu |

En actualitzar des de l'upstream, només cal re-aplicar aquests 5 punts (cerca `collab-fork`).

## Limitacions conegudes (decisions conscients)

- **Un sol worker**: l'estat de ronda viu en memòria del procés (`_active_rounds`). Per a multi-worker caldria moure'l a Redis — no aplica en ús local.
- **Torns seqüencials**: només un agent treballa alhora (evita conflictes d'edició). Paral·lelisme amb worktrees queda com a millora futura.
- **Threads**: la taula rodona és lineal per disseny; els threads dels canals segueixen disponibles per a converses humanes, però els agents no n'obren.
- **Backend sense `--reload`**: a Windows el mode reload d'uvicorn trenca els subprocessos dels CLI (vegeu DEVLOG 2026-07-16). Reinicia el backend a mà quan toquis Python.
- **Eines vs CLI**: els pipes CLI (Claude/Codex) no fan tool-calling d'Open WebUI; interactuen amb el projecte via `cwd` i proposen tancament amb el marcador `FEINA_ACABADA:`. Les eines (`collab_files`, tasques, `propose_finish`) són per a la resta de models.

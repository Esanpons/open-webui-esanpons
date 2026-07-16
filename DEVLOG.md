# DEVLOG — Registre de desenvolupament (fork esanpons)

Registre cronològic de canvis, errors resolts i millores del fork.
Entrada més recent a dalt. Format de data: AAAA-MM-DD.

---

## 2026-07-16

### 📜 Historial complet consultable + defaults de guardarails de l'Esteve
- **Historial (petició Esteve):** el context per torn és una finestra (`context_messages`, canviable en calent), però TOTA la conversa queda guardada al canal. Nou `collab/history.py` + tool v3 amb **`read_conversation(offset, limit)`** i **`search_conversation(query)`**: un agent pot revisar tot el que s'ha fet o buscar decisions antigues quan ho cregui o quan l'usuari li demani. Anunciat al system prompt dels torns.
- **Defaults nous** (elecció Esteve): `allow_self_reply` on, `auto_summary` off. La resta igual (max_agent_turns 0, repòs on, timeouts 900/180, context 30, planificació on, límit ronda 0).
- **Filosofia +1 punt** (sorgit de revisar la seva taula real): "l'usuari és un membre més: si el pla preveu que validi alguna cosa, demaneu-l'hi i ESPEREU la seva resposta abans d'executar aquella part" — a la seva sessió del iogur, el pla deia "Esteve validarà abans d'executar" però no el van esperar.
- Revisió de la sessió real "web de iogur" (Sonnet + Codex Terra): planificació→pregunta clau (qui genera les imatges)→PLA_ACORDAT amb repartiment→vot→Codex genera 2 imatges IA reals (2MB)→anuncia→Sonnet integra HTML/CSS→demana revisió→FEINA_ACABADA→consens→📊 4 torns/5m50s. Flux d'equip complet i correcte. ✅

### 🔄 Treball CONTINU: l'equip no s'atura esperant botons (petició de l'Esteve)
- **Motiu:** "no vull que sigui algo que tingui que anar fent jo... ha de ser més dinàmic, com si fos un equip, no un robot que algú ha de donar a un botó". Les "rondes" que es tancaven i esperaven un ▶ humà trencaven la sensació d'equip.
- **Canvis:**
  - **`max_agent_turns` per defecte = 0 (sense límit)**: l'equip treballa en continu fins al consens de FEINA_ACABADA, el repòs per inactivitat real o una aturada manual. El guardarail queda com a fre opcional.
  - **Empentes anti-silenci**: si ningú aixeca la mà però queda feina (pla no acordat / tasques obertes / falta proposar el tancament), el sistema NO tanca: dona un torn forçat al següent agent en rotació amb un avís del sistema segons la situació. Només després de 2 empentes consecutives sense reacció l'equip queda **😴 en repòs** ("escriu qualsevol missatge per reactivar-lo").
  - **Llenguatge**: "sessió de treball" en lloc de "ronda"; xip "equip treballant"; botons "▶ Posa l'equip a treballar" / "⏹ Atura l'equip"; avisos ⏸/⏱/📊 actualitzats.
- El paper de l'usuari queda reduït al que volia l'Esteve: donar l'objectiu, intervenir quan vulgui, i el botó d'aturada com a fre d'emergència.

### 🧭 Filosofia d'equip: PRIMER planificar junts, DESPRÉS executar (petició de l'Esteve)
- **Motiu:** l'Esteve vol que funcionin com un EQUIP UNIT — no "el primer tira i la resta segueix": primer parlen l'objectiu entre tots, consensuen el pla, es reparteixen la feina, i si un depèn d'un altre, espera i l'altre li anuncia quan acaba. Inspirat en la seva nota "Negocios del Reino" (2019): sense piràmide, funcions en lloc de rangs, submissió mútua, mentoria dels nous.
- **Implementació:**
  - **Fases d'equip** (`collab/tasks.py: get/set_phase`, a `channel.meta['collab_phase']`): 📋 `planning` → 🔨 `execution`. En planificació els agents NO toquen fitxers: pregunten, proposen, critiquen i omplen el tauler. Es passa a execució quan algú proposa `PLA_ACORDAT: <pla>` (marcador universal, com FEINA_ACABADA) i la resta ho **vota** favorablement (mateix mecanisme de consens, redactat per a plans). En tancar amb FEINA_ACABADA, l'espai torna a 📋 per al proper objectiu.
  - **Bloc de filosofia** (`_PHILOSOPHY` a l'orquestrador) injectat a TOTS els prompts (torn, mà alçada, vot): sense jerarquia, submissió mútua, planificar primer, esperar dependències i anunciar quan s'acaba.
  - **Mà alçada conscient de fase**: en execució es diu explícitament "si estàs esperant una tasca d'un altre, NO intervinguis".
  - Guardarail nou **`require_planning`** (on per defecte; off = mode lliure d'abans). Comanda `/collab phase plan|exec` i camp `phase` a l'API per forçar la fase. **Xip de fase al panell** (📋 planificant / 🔨 executant).
  - Millora UX: els torns en curs mostren "⏳ *treballant…*" (abans quedaven buits — Codex pot trigar minuts i semblava mort).
- **Nota de la prova real de l'Esteve:** la seva primera taula (Codex Luna + Claude Haiku, "feu una web") va acabar amb 5 torns/8m52s i revisió creuada real (Haiku va arreglar el menú mòbil de Codex i Codex va verificar el fix) — ja treballaven en equip; ara a més planificaran abans.

### 🏁 Espai col·laboratiu — PLA COMPLET (Fases 4+5) + ronda real verificada de cap a cap
- **Fase 4:**
  - **Tauler de tasques compartit** (`collab/tasks.py`, a `channel.meta['collab_tasks']`): eines `list_tasks`/`create_task`/`update_task` per als agents (tool `collab_files` v2), secció al panell per a l'usuari, injectat al context de cada torn. Endpoints REST `/tasks`.
  - **Consens explícit:** eina `propose_finish(resum)` o marcador de text `FEINA_ACABADA: <resum>` (universal — també per als pipes CLI sense tool-calling) → vot de la resta (majoria estricta, el proposant no vota) → tancament amb resum final.
  - **Resum incremental** (guardarail `auto_summary`): un agent fa de secretari en acabar cada ronda; el resum es guarda i es reinjecta (context contingut). Visible al panell.
  - Descarts conscients documentats: threads d'agents (la taula és lineal), Redis multi-worker (ús local).
- **Fase 5:** stats de ronda al canal ("📊 X torns · Y crides · temps"), guardarail nou `max_round_seconds`, mode `COLLAB_ADMIN_ONLY`, errors d'agent visibles al canal sense bloquejar la ronda, README **`docs/collab-workspace.md`** (guia + arquitectura + limitacions) i llista definitiva dels **5 hooks `[collab-fork]`** al nucli.
- **2 bugs més trobats i arreglats pel camí:**
  - Els pipes CLI (`claude_cli_pipe`, `codex_pipe`) **descartaven el missatge system** → les instruccions de la taula (identitat, regles, `FEINA_ACABADA`, arbre) no arribaven als agents. Ara es prepèn al prompt (`[Instruccions del sistema]`).
  - `_make_channel_emitter` (socket/main.py) només entenia el format llegat `{content, done}` → **els missatges dels models als canals quedaven buits** amb el middleware nou. Ara entén deltas OpenAI (`choices[].delta`), respostes completes i `output` items, amb acumulació. (Això arregla també les mencions normals de models als canals.)
- **Verificació amb agents reals** (Claude Sonnet + Haiku via CLI, backend de proves): objectiu → Sonnet aixeca la mà, **crea el fitxer**, s'adreça a Haiku, proposa `FEINA_ACABADA` → vot → **consens ✅** → resum final + avís 🗂️ + 📊 stats + resum incremental desat. Cicle complet en 22 segons. A més: unit del tool v2 ✅ i E2E REST 22/22 ✅ (inclou tasques).

### 🐛 FIX CRÍTIC: la taula rodona tancava les rondes a l'instant ("cap agent té res a afegir")
- **Símptoma:** en escriure al canal, la ronda es tancava immediatament sense que cap agent parlés.
- **Causa (logs):** els dos pipes petaven amb `NotImplementedError` a `asyncio.create_subprocess_exec` — el backend s'havia arrencat amb **`uvicorn --reload`**, i a Windows el procés fill del reloader corre amb un event loop que NO suporta subprocessos. Els CLI de Claude/Codex no es podien ni llançar. **Regla per a aquesta màquina: MAI arrencar el backend amb `--reload`** (contrapartida: cal reiniciar-lo a mà quan es toca codi Python).
- **Millores fetes de passada:**
  - Orquestrador: ara distingeix "no vull intervenir" (consens) d'un **error** del model; si TOTS fallen, publica "⚠️ Cap agent ha pogut respondre..." al canal en lloc del confús "cap agent té res a afegir".
  - Pipes: les crides de **mà alçada són one-shot** (sense `--session-id`/`resume`) per no embrutar la sessió de la conversa del canal; la clau de sessió dels torns usa el `channel_id` del context collab com a fallback.
  - **Sincronització automàtica dels pipes a la BD**: el contingut de la taula `function` (ids `claude_code`, `codex_chatgpt_plus`) s'ha actualitzat directament des d'`integrations/*.py` — ja no cal reenganxar codi a la UI de Functions.
  - Panell UI: els guardarails ara tenen **etiqueta en català + tooltip explicatiu** (hover), i la carpeta-projecte es pot **escriure a mà** (input + Usa) a més del navegador de carpetes.
- **Verificat:** completion no-streaming real (mateix camí que el hand-raise) contra el pipe `claude_code.haiku` en un backend sense `--reload` → respon `ok`. ✅

### 🚀 Espai col·laboratiu — Fases 2+3 COMPLETES: fitxers externs als models + API REST + panell UI 🤝
- **Decisió d'arquitectura (petició de l'Esteve):** la gestió d'arxius NO pot dependre dels pipes de Claude/Codex — quan s'afegeixin models estàndard (Ollama, APIs) també han de poder llegir/escriure el projecte. Solució: **tot extern als models**:
  - **Eina `collab_files`** (`collab/file_tools.py`): Tool estàndard d'Open WebUI auto-registrat a la BD (idempotent, versionat) i adjuntat a cada torn via `tool_ids` → `list_project_files()`, `read_project_file(path)`, `write_project_file(path, content)`. Funciona amb el tool-calling natiu del middleware, per a QUALSEVOL model. La carpeta arriba per `__metadata__['variables']['collab']`. Rutes segures (`collab/files.py: resolve_safe`, sense traversal, límits de mida).
  - **Arbre de fitxers injectat** al system prompt de cada torn (fitat a 150 entrades) → tots els agents "veuen" el projecte encara que no cridin eines.
  - **Detecció de canvis**: snapshot mtime+mida abans/després de cada torn → avís al canal "🗂️ X ha tocat el projecte: 🆕 fitxer / ✏️ fitxer / 🗑️ fitxer". Detecta canvis fets per QUALSEVOL via (eines, CLI amb cwd, o l'usuari a mà).
  - El `cwd` dels pipes CLI es manté com a bonus (Claude Code/Codex treballen més bé natiu), però ja no és el mecanisme del sistema.
- **API REST** `collab/router.py` muntada a `/api/v1/collab` (2n i últim hook al nucli, marcat `# [collab-fork]` a `main.py`): GET/POST config (validacions: agents, carpeta, mode, guardarails), GET files (arbre) + files/content (visor), GET browse (navegador de carpetes per al selector; respecta `COLLAB_ALLOWED_ROOTS`, sense llista només admin), POST start/stop de ronda.
- **UI**: `src/lib/components/collab/CollabPanel.svelte` + client `src/lib/apis/collab/index.ts`. Botó flotant 🤝 a `Channel.svelte` (únic fitxer core del frontend tocat, marcat `[collab-fork]`; el panell segueix el mateix patró Pane/Drawer que els Threads). El panell fa TOTA la configuració sense comandes: activar espai, triar agents del desplegable de models, **selector visual de carpeta**, mode, guardarails editables, ▶/⏹ ronda, i arbre de fitxers en viu (refresc amb sockets del canal) amb visor de contingut. Les comandes `/collab` segueixen funcionant en paral·lel.
- **Verificat:** unit del tool (specs OK, `__metadata__` ocult al model, read/write/list, traversal bloquejat) ✅ · E2E API REST 16/16 ✅ · E2E comandes `/collab` 11/11 (regressió) ✅ · `CollabPanel.svelte` i `Channel.svelte` compilen nets a Vite. Pendent: prova amb agents reals (gasta quota).
- **Fix entorn dev:** `channels.enable` estava persistit a `false` a la BD (`config` per-key) i la env `ENABLE_CHANNELS` no el sobreescriu → posat a `true` directament a la BD.

## 2026-07-14

### 🚀 Feature: Espai de treball col·laboratiu multi-agent (taula rodona d'IAs) — Fase 1+2 backend
- **Què és:** diverses IAs (Claude, Codex, models API...) compartint un mateix channel i una carpeta-projecte, col·laborant sense jerarquia. Pla mestre amb fases i checkboxes: `docs/plans/espai-collaboratiu.md`.
- **Arquitectura:** tot el codi nou aïllat a `backend/open_webui/collab/` (`config.py`, `orchestrator.py`, `commands.py`). **Un únic hook al nucli**, marcat `# [collab-fork]`, dins `background_handler` de `post_new_message` (`routers/channels.py`): si el canal té `meta['collab']` actiu o el missatge és `/collab ...`, ho gestiona l'orquestrador i se salta el `model_response_handler` estàndard.
- **Funcionament:** amb el mode actiu, cada missatge humà obre una **ronda**: es pregunta a cada agent (crida curta no-streaming, JSON `{"intervene", "priority", "reason"}`) si vol intervenir (*hand-raising*), i parlen per torns **seqüencials** (evita conflictes d'edició) fins que ningú té res a afegir, `/collab stop`, o un guardarail. Cada torn reutilitza el pipeline complet (`CHAT_COMPLETION_HANDLER` amb `chat_id=channel:...` → streaming al canal via `_make_channel_emitter`); es detecta el final pel flag `meta.done` del missatge.
- **Guardarails 100% configurables per espai** (cap límit hardcodejat, canviables en calent): `max_agent_turns`, `end_on_silence`, `allow_self_reply`, `turn_timeout`, `handraise_timeout`, `context_messages`. `/collab guardrails clau=valor` (0/off = desactivat).
- **Agents per taula:** `/collab agents @Claude @Codex ...` (mencions o ids), `add`/`remove` en calent. Cada taula pot tenir agents diferents.
- **Carpeta-projecte (estil CLI):** `/collab dir <ruta>` — els agents CLI treballen amb aquesta carpeta com a `cwd`, com obrir `claude`/`codex` des d'allà al terminal. Seguretat: env `COLLAB_ALLOWED_ROOTS` (llista blanca `;`); sense llista, només admin. El context arriba als pipes via `__metadata__['variables']['collab']` (torns) o `__metadata__['collab']` (hand-raise) — `main.py` passa `variables` dins `metadata` sense tocar res del nucli.
- **Pipes actualitzats** (els 3 d'`integrations/` — cal reinstal·lar-los a la UI de Functions!): helper `_collab_ctx()`, `cwd=project_dir`, sessions per `(chat_id, project_dir)`. `codex_pipe`: valve nova `COLLAB_SANDBOX` (default `danger-full-access` NOMÉS quan hi ha carpeta-projecte, perquè el sandbox natiu falla sota AzureAD; xats normals segueixen read-only). `claude_agent_pipe`: fix de bug latent — el workdir per-chat amb `chat_id` tipus `channel:xxx` conté `:` (invàlid a Windows) → `_safe_dir_name()`.
- **Comandes:** `/collab help | status | on | off | agents | dir | mode handraise|roundrobin | guardrails | start | stop`.
- **Verificat:** compilació + imports OK (sense circulars); E2E real contra backend en marxa (DB temporal, port 8081): les 11 proves de comandes passen (help, status, validacions d'agents/dir, guardarails persistits i en calent, mode, stop, missatge normal sense resposta amb collab off). ⚠️ Falta la prova amb agents reals (gasta quota) — Fase 0/1 del pla, fer-la manualment.
- **Gotcha trobat:** la feature Channels ve DESACTIVADA per defecte (`ENABLE_CHANNELS`); s'activa a Admin Settings o via `POST /api/v1/auths/admin/config` amb `ENABLE_CHANNELS: true`. El valor queda persistit a la config de la BD (l'env només val com a default inicial).
- **Limitació coneguda:** l'estat de ronda (`_active_rounds`) és en memòria → un sol worker (OK en local; multi-worker necessitaria Redis).

### ➕ Millora: effort via paràmetre NATIU d'Open WebUI (selector net) — Claude i Codex
- **Motiu:** tenir 12 opcions (model×effort) al selector era carregós. Millor: 4 models nets + effort a part, com fan les apps de Claude/ChatGPT.
- **Descoberta:** Open WebUI té un paràmetre natiu `reasoning_effort` (Chat Controls → Advanced Params, o als params del model). Arriba al `body` que rep el pipe (`functions.py:194` passa `form_data` sencer; `payload.py:124` el mapeja).
- **Canvi:** els dos pipes (`claude_cli_pipe.py`, `codex_pipe.py`) exposen ara **només els models** a `pipes()`. `_resolve_choice()` llegeix l'effort de `body["reasoning_effort"]` (validat contra la llista de nivells), amb fallback a la Valve `EFFORT`.
- **On es tria l'effort a la UI:** obre **Chat Controls** (icona a dalt dreta del xat) → **Advanced Params** → activa "Reasoning Effort" i escriu `low`/`medium`/`high` (Codex) o fins a `xhigh`/`max` (Claude).
- Selectors resultants: Claude = Opus/Sonnet/Haiku/Fable · Codex = Sol/Terra/Luna/5.5.
- Verificat: resolució (model, effort) correcta en tots els casos. ✅

### ➕ Millora: Claude via CLI (`claude -p`) — SENSE token OAuth
- **Motiu:** el pipe original de Claude (`claude_agent_pipe.py`) requereix generar i enganxar un token OAuth (`claude setup-token`). Com que el CLI `claude` ja està logat a la màquina, es pot fer com Codex: cridar el CLI directament, sense token.
- **Solució:** pipe nou `integrations/claude_cli_pipe.py` (bessó del de Codex) que fa `claude -p --permission-mode bypassPermissions` amb el prompt per STDIN. Patró copiat del `SaPa-Connect/app/services/ai.py` (línia ~98).
- **Avantatges:** sense token a manejar, sense dependències pip (`claude-agent-sdk`), consistent amb el pipe de Codex, ~180 línies vs 1600.
- Exposa models `opus` / `sonnet` / `haiku` / `fable` × effort `low` / `medium` / `high` (12 opcions al selector, igual patró que Codex). El CLI de Claude té `--effort` (nivells: low/medium/high/xhigh/max). Sessions per xat via `--session-id <uuid>` + `--resume`.
- **Verificat:** respon "Hola, funciono." amb Sonnet i amb Fable+low, sense token. ✅
- El pipe antic (`claude_agent_pipe.py`, agent complet amb eines) es pot desactivar o mantenir per a tasques agèntiques.

### ➕ Millora: Codex exposa Sol/Terra/Luna + selecció d'effort al selector
- CLI de Codex actualitzat 0.139.0 → **0.144.4** (`npm i -g @openai/codex@latest`). Els models GPT-5.6 (Sol/Terra/Luna) requereixen CLI >= 0.144; amb 0.139 donaven error 400 "requires a newer version".
- Verificat que amb compte Plus funcionen: `gpt-5.6-sol` (flagship, effort medium+), `gpt-5.6-terra` (equilibri), `gpt-5.6-luna` (ràpid), més `gpt-5.5` (fallback).
- `pipes()` genera ara una entrada per cada combinació **model × effort** (12 opcions: "Codex Sol (high)", "Codex Terra (low)"...). Així es tria model i effort d'un cop des del selector del xat.
- `_resolve_choice()` descodifica l'ID `<model>__<effort>`. ⚠️ No es pot fer `split(".")` perquè els IDs de model tenen punts (gpt-5.6-sol) → es fa match contra la llista coneguda de models.

### ➕ Integració: Codex (ChatGPT Plus) via pipe `codex exec` — SENSE proxy ni API
- **Objectiu:** usar la subscripció **ChatGPT Plus** a Open WebUI (tokens del compte, NO API de pagament).
- **Descoberta clau:** cap function de la comunitat ho resol (rebutjat oficialment per risc de ToS — veure discussió open-webui #25122). L'única via és cridar el CLI oficial `codex exec`, que usa la sessió `~/.codex/auth.json` (compte ChatGPT).
- **Solució:** pipe nou `integrations/codex_pipe.py` que fa `codex exec` per darrere, inspirat en la skill `parlar-amb-codex` i el servei `SaPa-Connect/app/services/ai.py` de l'Esteve (que ja fan això i funcionen).
- **Detalls tècnics importants:**
  - Prompt per **STDIN** (`-`), no com a argument (robust a Windows / cometes / longitud).
  - A Windows el `codex` és un shim `.CMD` que subprocess no pot executar → es crida `node codex.js` directament.
  - **Model:** `gpt-5.5` (el defecte del CLI `gpt-5.6-sol` i `gpt-5.1` NO són compatibles amb compte ChatGPT → error 400). Amb ChatGPT login només serveixen certs models.
  - Sessions per xat: es captura el `session id: <uuid>` del stdout i s'usa `codex exec resume <sid>` als torns següents.
  - Sandbox `read-only` (xat de text; el sandbox complet està trencat en aquesta màquina AzureAD → `CreateProcessAsUserW failed: 5`).
- **Verificat:** el pipe respon "hola, funciono." gastant la subscripció Plus. ✅
- ⚠️ **Nota ToS:** usar la subscripció ChatGPT fora de les apps oficials pot violar els termes d'OpenAI. Ús personal, sota el teu criteri.


### ✅ Fix: pipe de Claude Code fallava a cada xat — `not enough values to unpack (expected 3, got 2)`
- **Símptoma:** en xatejar amb qualsevol model "Claude Code", la resposta era només l'error `not enough values to unpack (expected 3, got 2)`, sense text.
- **Causa (bug del pipe original):** la funció `_build_kb_mcp_server()` retornava només 2 valors (`return None, []`) quan no hi ha cap knowledge base adjunta, però els 3 llocs que la criden desempaqueten 3 valors. Es disparava a cada xat sense KB (el cas normal).
- **Solució:** `integrations/claude_agent_pipe.py:383` → `return None, [], {}`.
- **Com s'ha depurat:** carregant el pipe via `importlib` dins l'entorn conda i cridant `pipe.pipe(body=...)` directament amb un token dummy — reprodueix el traceback real sense passar per l'auth HTTP.
- **Verificat:** el pipe respon correctament ("¡Hola! 👋 ...") amb Thinking i cost. ✅

### ➕ Millora: pipe de Claude Code exposa els 3 models al selector
- `pipes()` retorna ara Opus 4.7 / Sonnet 4.6 / Haiku 4.5 (abans només un model fix).
- `pipe()` llegeix el model triat de `body["model"]` (format `<function_id>.<pipe_id>`) i el passa a l'SDK, ignorant la valve `MODEL`.
- Fitxer: `integrations/claude_agent_pipe.py`.

### 🔧 Fix Windows: `WORKDIR_ROOT` del pipe usava ruta Linux
- El valor per defecte era `/tmp/claude-agent-pipe` (Linux) → fallaria a Windows.
- Canviat a `tempfile.gettempdir()` (ruta temp correcta a Windows). Afegit `import tempfile`.

### 🐛 Debug: afegit desplegable "Traceback" als errors del pipe
- Al bloc `except` del pipe s'afegeix un `<details>Traceback` amb el traceback complet, per depurar errors des del xat.

### ➕ Integració: Claude (compte Max) via pipe de Functions
- Objectiu: usar la subscripció **Claude Max** (tokens del compte, NO API de pagament).
- Pipe de `github.com/tfriedel/openwebui-claude-code` → `integrations/claude_agent_pipe.py`.
- Requereix `claude-agent-sdk` + `anthropic` (instal·lats a l'entorn conda `open-webui`) i la CLI `claude`.
- Auth: `claude setup-token` genera un token OAuth `sk-ant-oat...` que es posa a la valve `CLAUDE_CODE_OAUTH_TOKEN`.
- ⚠️ Configurat en **mode agent complet** (bypassPermissions + Bash/Write/Edit): Claude pot actuar al PC des del xat.

### 🛠️ Script d'arrencada: `scripts/dev-start.ps1`
- Arrenca backend (conda `open-webui`, :8080) + frontend (Vite, :5173) alhora.
- En tancar (Ctrl+C o tancar finestra) apaga tots dos processos i els seus fills.
- Opcions: `-NoBrowser`, `-BackendPort`, `-FrontendPort`, `-CondaEnv`.

### 🚀 Entorn d'execució local (setup inicial)
- **Problema:** Python 3.14 i Node 26 del sistema massa nous (deps requereixen Python `>=3.11,<3.14`; frontend Node `<=22`).
- **Backend:** entorn conda aïllat `open-webui` amb Python 3.11 (`conda create -n open-webui python=3.11`). Deps: `pip install -r backend/requirements.txt`.
- **Frontend:** `npm install --engine-strict=false` (per saltar el límit de Node).
- Backend a http://localhost:8080, frontend a http://localhost:5173.

---

## Pendent / TODO
- [ ] Integrar **Codex** (compte ChatGPT Plus) a Open WebUI via pipe.
- [ ] (Opcional) Arrencar Ollama (`ollama serve`) per a models locals gratuïts.
- [ ] (Opcional) Treure el desplegable "Traceback" de debug del pipe un cop tot estable.

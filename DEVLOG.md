# DEVLOG — Registre de desenvolupament (fork esanpons)

Registre cronològic de canvis, errors resolts i millores del fork.
Entrada més recent a dalt. Format de data: AAAA-MM-DD.

---

## 2026-07-14

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

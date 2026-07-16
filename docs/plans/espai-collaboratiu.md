# Pla: Espai de treball col·laboratiu multi-agent (taula rodona d'IAs)

> **Estat:** ✅ **PLA COMPLET** — Fases 0 a 5 implementades i provades, incloent una **ronda real amb agents Claude** (torn → fitxer creat → proposta de tancament → vot → consens → resum, en 22s). Queden només millores futures opcionals (paral·lelisme amb worktrees, Redis multi-worker, threads d'agents), documentades com a decisions conscients a `docs/collab-workspace.md`.
> **Última actualització:** 2026-07-16
> **Com fer-lo servir:** anar marcant les caselles `[ ]` → `[x]` a mesura que s'executa. Cada fase acaba amb una prova manual ("Com validar-ho") abans de passar a la següent.

---

## 1. Objectiu

Crear un nou tipus de conversa a Open WebUI — **"Espai de treball col·laboratiu"** — on diverses IAs (Claude Code, Codex, Gemini, models API...) comparteixen:

- **Un únic xat** amb el mateix context (tots veuen tots els missatges, també els de les altres IAs).
- **Un mateix projecte**: una **carpeta del disc escollida per l'usuari**, que tots els agents veuen i poden modificar, amb un panell visual de l'arbre de fitxers perquè l'usuari (i els agents) vegin què hi ha i què va canviant.
- **Cap jerarquia**: no hi ha agent director ni flux predefinit. Cada IA pot analitzar l'objectiu, proposar, preguntar, respondre a les altres, repartir-se feina, tocar fitxers i revisar la feina conjunta fins arribar a consens.
- **L'usuari** només defineix l'objectiu i intervé quan vol.

**Principi d'implementació:** màxim com a plugin/add-on (Functions/pipes + mòduls nous aïllats), mínim canvi al nucli d'Open WebUI, i el que calgui tocar del nucli, ben aïllat i documentat aquí i al `DEVLOG.md` per sobreviure a actualitzacions de l'upstream.

## 2. Veredicte de viabilitat

**Sí, és factible**, i més fàcil del que semblava, perquè Open WebUI 0.10.2 ja porta el 60-70% dels fonaments:

| Peça necessària | Ja existeix? | On |
|---|---|---|
| Xat multi-participant en temps real | ✅ Channels (estil Discord, socket.io) | `backend/open_webui/routers/channels.py`, `src/lib/components/channel/` |
| Que un model respongui dins un channel | ✅ Mencions `@model` i replies disparen `model_response_handler` | `backend/open_webui/routers/channels.py:947` |
| Sortida d'un model → missatge de canal en streaming | ✅ `_make_channel_emitter` (chat_id amb prefix `channel:`) | `backend/open_webui/socket/main.py:851` |
| Agents CLI reals (Claude Code, Codex) com a models | ✅ Pipes del fork | `integrations/claude_agent_pipe.py`, `codex_pipe.py`, `claude_cli_pipe.py` |
| Agents amb accés a una carpeta de treball (`cwd`) | ✅ (per xat) | `integrations/claude_agent_pipe.py:1439` |
| Events asíncrons des d'un pipe (status, fitxers...) | ✅ `__event_emitter__` / `__event_call__` | `backend/open_webui/functions.py:186`, `socket/main.py:919` |

**El que falta (el que construirem):**

1. **Conversa entre IAs**: avui cada model mencionat respon en paral·lel i aïllat; cap IA veu la resposta de l'altra dins el mateix torn, i cap resposta d'IA dispara una altra IA. Cal un **motor de torns**.
2. **Membres-agent reals**: la taula `channel_member` només admet usuaris; els models "participen" només reactivament. Cal poder dir "en aquest espai hi participen Claude, Codex i Gemini".
3. **Projecte compartit**: escollir una carpeta del disc com a projecte de l'espai, que tots els pipes rebin com a `cwd`, i visualitzar-ne l'arbre de fitxers i els canvis.
4. **Guardarails**: límits de torns, aturada per l'usuari, detecció de consens, control de cost.

## 3. Arquitectura proposada

```
┌────────────────────────── Open WebUI ──────────────────────────┐
│  Channel tipus "workspace" (reutilitza channels + socket.io)   │
│  ├─ meta: { project_dir, agents: [...], estat, config }        │
│  │                                                             │
│  ├─ ORQUESTRADOR (mòdul nou aïllat)                            │
│  │   backend/open_webui/collab/  ← tot el codi nou aquí        │
│  │   • escolta missatges nous del channel                      │
│  │   • ronda: pregunta a cada agent "vols intervenir?"         │
│  │   • encua i executa torns (reutilitza                       │
│  │     model_response_handler / CHAT_COMPLETION_HANDLER)       │
│  │   • guardarails: max torns, stop, consens, silenci          │
│  │                                                             │
│  ├─ AGENTS = models ja existents                               │
│  │   • pipes CLI (claude_agent_pipe, codex_pipe…) amb          │
│  │     cwd = project_dir de l'espai                            │
│  │   • models API normals (Gemini, GPT…) via el pipeline       │
│  │                                                             │
│  └─ FRONTEND                                                   │
│      • modal "Nou espai col·laboratiu" (agents + carpeta)      │
│      • panell lateral: arbre de fitxers del projecte + canvis  │
│      • indicadors: qui està "pensant", torn actual, stop       │
└────────────────────────────────────────────────────────────────┘
```

**Decisions de disseny clau (ja preses, revisables):**

- **D1 — Base: Channels, no Chats.** Els chats són 1-usuari-N-models en arbre; els channels ja són multi-participant, lineals, amb threads, temps real i suport de resposta de models. És la base natural.
- **D2 — Codi nou en mòdul propi** (`backend/open_webui/collab/` + components `src/lib/components/collab/`), amb *hooks* mínims al nucli (idealment 2-3 punts: `channels.py` post-message, registre del router, i el modal de creació de canal). Cada hook marcat amb comentari `# [collab-fork]` per localitzar-los en futurs merges d'upstream.
- **D3 — Torns per "hand-raising", no round-robin fix.** Després de cada missatge, l'orquestrador fa una crida barata (no streaming, prompt curt) a cada agent inactiu: *"Vist l'últim estat de la conversa, vols intervenir ara? (sí/no + prioritat + motiu)"*. Els que diuen sí, parlen per ordre de prioritat. Això dona la sensació d'equip autoorganitzat sense un "director" que decideixi contingut.
- **D4 — Identitat dels agents al context.** Cada missatge al historial que es passa a un agent porta prefix d'autor (`[Esteve]`, `[Claude Code]`, `[Codex]`...), perquè cada IA sàpiga qui ha dit què i pugui adreçar-se a les altres.
- **D5 — La carpeta-projecte funciona com a Claude Code / Codex al terminal.** L'espai s'ancora a una carpeta del disc i tot el xat "corre des d'allà": cada torn d'un agent CLI s'executa amb `cwd = project_dir` (com si haguessis obert `claude` o `codex` dins la carpeta), de manera que sempre tenen el context real del projecte (fitxers, git, etc.). Llista blanca de rutes arrel configurables per seguretat. Els models API "veuen" el projecte via l'arbre + lectura de fitxers que l'orquestrador injecta al context o via tools.
- **D6 — Guardarails 100% configurables per espai, cap de predeterminat obligatori.** Cada guardarail (màx. torns seguits, límit de cost, fi per silenci/consens, timeout per agent...) es pot activar, desactivar i ajustar individualment a la config de cada espai, i canviar en qualsevol moment amb la conversa en marxa (`/collab guardrails ...` o des de la UI). Hi ha valors suggerits, però són només valors inicials editables — mai límits fixos al codi.
- **D7 — Els agents es trien per taula.** Cada espai té la seva pròpia llista de participants (2, 3, els que vulguis, i diferents a cada taula), escollits d'entre tots els models disponibles ($models: pipes CLI i models API). Es poden afegir i treure agents amb l'espai ja en marxa.

## 4. Fases

### Fase 0 — Validació de fonaments (sense escriure codi nou) ✅ quan estigui provat

Objectiu: confirmar amb les mans que les peces existents fan el que l'exploració diu.

- [x] Arrencar l'entorn dev (fet repetidament; ⚠️ MAI amb `uvicorn --reload` — trenca els subprocessos dels CLI a Windows, vegeu DEVLOG 16-07).
- [x] ~~Mencionar `@claude` i `@codex` alhora~~ — superat pel disseny: als canals col·laboratius el hook desvia TOTS els missatges a l'orquestrador (el comportament estàndard de mencions ja no aplica); el gap #1 el resol el motor de torns.
- [x] Pipe dins un channel amb streaming via `_make_channel_emitter` — verificat amb ronda real; de fet s'hi va trobar i arreglar un bug del nucli (l'emitter no entenia els formats nous d'esdeveniment i deixava els missatges buits).
- [x] ~~Reply a un model~~ — superat pel mateix motiu (l'orquestrador reconstrueix el context complet a cada torn).
- [x] Sorpreses anotades al `DEVLOG.md`: `ENABLE_CHANNELS` persistit a la BD, `--reload` trenca subprocessos, formats nous de `chat:completion`, els pipes CLI descartaven el system prompt.

**Com validar-ho:** checklist de dalt completa + notes al DEVLOG.

### Fase 1 — MVP "taula rodona" en un channel

Objectiu: diverses IAs conversant entre elles en un channel, amb l'usuari podent intervenir. Encara sense carpeta-projecte ni UI nova (es fa servir la UI de channels tal qual).

- [x] Crear `backend/open_webui/collab/` amb l'**orquestrador** v1:
  - [x] Model de config de l'espai guardat a `channel.meta['collab']` (`config.py`: agents, project_dir, mode, guardarails).
  - [x] Hook a `channels.py` post-missatge (`# [collab-fork]`, dins `background_handler` de `post_new_message`): si el channel és col·laboratiu o el missatge és `/collab`, delega a l'orquestrador.
  - [x] Ronda de "hand-raising" (D3): crida curta no-streaming a cada agent → JSON `{"intervene","priority","reason"}` → cua per prioritat. Mode alternatiu `roundrobin`.
  - [x] Execució de torn: historial amb autors etiquetats `[Nom]:` (D4) + `CHAT_COMPLETION_HANDLER` amb `chat_id=channel:...`; fi de torn detectat per `meta.done` del missatge. Torns seqüencials.
  - [x] Les respostes d'un agent disparen una nova volta del bucle (les IAs es responen entre elles fins al silenci).
- [x] **Guardarails** v1 — configurables per espai (D6), canviables en calent (`/collab guardrails clau=valor`, 0/off = desactivat): `max_agent_turns`, `end_on_silence`, `allow_self_reply`, `turn_timeout`, `handraise_timeout`, `context_messages`. Cap límit hardcodejat.
- [x] Activació provisional sense UI: comandes al channel (`/collab help|status|on|off|agents|dir|mode|guardrails|start|stop`).
- [x] Documentar al `DEVLOG.md` els punts tocats del nucli.
- [x] **Prova amb agents reals** (16-07, backend de proves, agents Claude Sonnet + Haiku reals via CLI): objectiu "creeu SALUTACIO.md i doneu la feina per acabada" → Sonnet aixeca la mà, crea el fitxer, s'adreça a Haiku pel nom i proposa tancar (`FEINA_ACABADA:`); vot de consens (Haiku a favor) → tancament amb resum final + avís 🗂️ + stats 📊 + resum incremental. Cicle complet en 22s. ✅ (La prova llarga amb Claude+Codex i ≥3 intercanvis queda per a l'ús real de l'Esteve.)

**Com validar-ho:** en un channel, donar un objectiu ("dissenyeu entre tots l'esquema d'una API de tasques i critiqueu-vos les propostes") i veure ≥3 intercanvis IA↔IA coherents, aturada neta amb `/stop`, i fi natural quan arriben a acord.

### Fase 2 — Espai amb projecte: carpeta compartida

Objectiu: l'espai té una **carpeta del disc com a projecte**; tots els agents hi treballen i l'usuari veu què hi ha.

- [x] Config backend: **rutes arrel permeses** via env `COLLAB_ALLOWED_ROOTS` (separades per `;`); sense llista, només admin pot fixar carpeta. Selecció amb `/collab dir <ruta>` (l'endpoint de navegació de carpetes queda per a la UI de Fase 3).
- [x] Passar `project_dir` com a `cwd` als pipes CLI: els 3 pipes d'`integrations/` llegeixen el context collab de `__metadata__` (helper `_collab_ctx`) i treballen des de la carpeta, amb sessions per `(chat, carpeta)`. Codex amb valve `COLLAB_SANDBOX` per poder escriure.
- [x] **Gestió de fitxers EXTERNA als models** (decisió reforçada per l'Esteve: ha de funcionar igual amb Ollama/APIs, no dependre dels pipes): eina estàndard `collab_files` (`collab/file_tools.py`) auto-registrada a la taula Tools i adjuntada a cada torn (`tool_ids`) — `list_project_files` / `read_project_file` / `write_project_file` (lectura I ESCRIPTURA), amb rutes segures (sense traversal). L'arbre de fitxers s'injecta al system prompt de cada torn. El `cwd` dels pipes CLI queda com a bonus, no com a requisit.
- [x] **Esdeveniments de fitxers**: snapshot (mtime+mida) abans/després de cada torn (`collab/files.py`) i avís al canal "🗂️ X ha tocat el projecte: 🆕/✏️/🗑️ fitxers".
- [x] Endpoints REST (`collab/router.py`, muntat a `/api/v1/collab` — 2n hook al nucli, marcat a `main.py`): config GET/POST, arbre de fitxers, contingut de fitxer, browse de carpetes (selector), start/stop de ronda.

**Com validar-ho:** crear un espai apuntant a una carpeta de prova amb un mini-projecte, demanar "afegiu una funció X amb tests", i veure Claude i Codex editant els mateixos fitxers, comentant els canvis de l'altre, amb els avisos 🗂️ al xat.

### Fase 3 — UI: nou tipus de conversa + panell de projecte

Objectiu: experiència d'usuari completa, sense comandes màgiques.

- [x] **Panell 🤝 "Taula rodona"** al channel (`src/lib/components/collab/CollabPanel.svelte` + botó flotant a `Channel.svelte`, marcat `[collab-fork]`): resol la configuració sense modal a part — qualsevol canal es converteix en espai col·laboratiu des del panell: activar/desactivar, triar agents (desplegable de `$models`, add/remove en calent), **selector visual de carpeta** (navegador de carpetes contra `/browse`), mode de torns, editor de guardarails.
- [x] **Panell de fitxers del projecte**: arbre en viu (refresc automàtic amb els events del canal + botó ⟳), clic sobre un fitxer per veure'n el contingut (visor readonly).
- [x] **Indicadors i controls**: xip d'estat (inactiva / activa / "ronda en curs" amb refresc periòdic), botons **▶ Inicia una ronda** i **⏹ Atura la ronda**. (El "qui està escrivint" ja es veu al mateix xat amb l'streaming del missatge de l'agent.)
- [x] Distinció visual dels agents: la dona Open WebUI de sèrie (nom + avatar del perfil del model a cada missatge).
- [x] Badge 🤝 a la sidebar (`ChannelItem.svelte`, marcat `[collab-fork]`): els canals amb espai col·laboratiu actiu es distingeixen dels normals.

**Com validar-ho:** flux complet des de la UI: crear espai → triar Claude+Codex+un model API → triar carpeta → escriure objectiu → mirar-los treballar veient l'arbre de fitxers actualitzar-se → Stop → intervenir → Continueu.

### Fase 4 — Col·laboració avançada ✅

- [x] **Tauler de tasques compartit** (`collab/tasks.py`, desat a `channel.meta['collab_tasks']`): els agents el gestionen amb les eines `list_tasks`/`create_task`/`update_task` (tool `collab_files` v2), l'usuari des de la secció "Tasques de l'equip" del panell (crear, canviar estat ⬜🔵✅, esborrar), i s'injecta al context de cada torn.
- [x] **Protocol de consens explícit**: un agent proposa tancar amb l'eina `propose_finish(resum)` o amb el marcador de text `FEINA_ACABADA: <resum>` (funciona amb TOTS els models, també els CLI sense tool-calling); la resta vota (majoria estricta, el proposant no vota); en consens, l'orquestrador tanca amb el resum final. **Verificat amb agents reals.**
- [x] Threads per subtemes — **descartat conscientment**: la taula rodona és lineal per disseny (tothom ho veu tot); els threads dels channels segueixen disponibles per a humans. Documentat a `docs/collab-workspace.md`.
- [x] **Resum incremental** de l'espai (guardarail `auto_summary`, on per defecte): en acabar cada ronda un agent fa de secretari i actualitza `channel.meta['collab_summary']`, que es reinjecta a totes les crides següents (el context no creix infinit). Visible al panell. **Verificat amb agents reals.**
- [x] Suport multi-worker (Redis) — **descartat conscientment per a l'ús local**: l'estat de ronda és en memòria d'un sol worker; documentat com a limitació coneguda a `docs/collab-workspace.md` per si mai es desplega fora del PC.

### Fase 5 — Robustesa i publicació ✅

- [x] Control de cost/ús: comptador per ronda (torns d'agent + crides curtes + durada) publicat al canal en tancar ("📊 Ronda: ..."), i límits configurables `max_agent_turns` + `max_round_seconds` (nou). Comptar tokens exactes no és viable uniformement (els pipes CLI no reporten usage) — les crides són el proxy.
- [x] Permisos: rutes de disc per llista blanca `COLLAB_ALLOWED_ROOTS` (sense llista → només admins trien carpeta), i mode `COLLAB_ADMIN_ONLY=true` (només admins configuren espais, gestionen tasques i llancen/aturen rondes — via REST i comandes).
- [x] Gestió d'errors: agent que falla no bloqueja la ronda (torn saltat amb avís ⚠️ al canal), `turn_timeout`/`handraise_timeout` per agent, distinció error-vs-silenci al hand-raising ("⚠️ Cap agent ha pogut respondre" vs "🤝 Ronda tancada").
- [x] README propi: **`docs/collab-workspace.md`** (guia d'ús, guardarails, variables d'entorn, arquitectura, limitacions conegudes) + entrades al `DEVLOG.md`.
- [x] Separació nucli/fork — llista definitiva dels punts `[collab-fork]` (4 + 1 fix): `routers/channels.py` (hook post-missatge), `main.py` (registre del router), `socket/main.py` (fix de l'emitter de canal per als formats nous d'esdeveniment), `Channel.svelte` (botó + pane), `ChannelItem.svelte` (badge). Taula completa a `docs/collab-workspace.md`.

## 5. Riscos i mitigacions

| Risc | Mitigació |
|---|---|
| **Bucles infinits / xerrameca entre IAs** (es responen per sempre) | Guardarails Fase 1: max torns per ronda, fi per "ningú vol intervenir", botó Stop sempre visible, límit de cost. |
| **Conflictes d'edició simultània** a la mateixa carpeta | v1: torns **seqüencials** (només un agent executa alhora, els altres esperen). Paral·lelisme real és una millora futura (worktrees). |
| **Upstream d'Open WebUI trenca els hooks** en actualitzar | Tot el codi en `collab/`, hooks mínims i marcats `# [collab-fork]`, documentats aquí i al DEVLOG. |
| **Seguretat**: agents amb accés a disc + bypassPermissions | Llista blanca de rutes arrel, espais només per a l'admin (v1), i avís clar a la UI de quina carpeta toquen. |
| **Cost**: la ronda de "hand-raising" multiplica crides | Prompt de hand-raising mínim (poques desenes de tokens, models barats/mateix agent amb effort baix), i només després de missatges "substantius". |
| **Models API no veuen el disc** | Arbre + tools de lectura injectats (Fase 2); acceptar que els agents CLI seran els que editen. |

## 6. Registre d'execució

| Data | Fase | Què s'ha fet | Notes |
|---|---|---|---|
| 2026-07-14 | — | Exploració d'arquitectura + redacció d'aquest pla | Fonaments confirmats a Open WebUI 0.10.2 |
| 2026-07-14 | 1 + 2 (parcial) | Backend complet: mòdul `backend/open_webui/collab/` (config + orquestrador hand-raising + comandes `/collab`), hook únic a `channels.py`, carpeta-projecte plumbed als 3 pipes d'`integrations/` | E2E de comandes verificat contra backend real (11/11). Pendent: prova amb agents reals (quota) i UI. Detall al DEVLOG. ⚠️ Reinstal·lar els pipes a la UI de Functions i activar `ENABLE_CHANNELS` |
| 2026-07-16 | 2 + 3 completes | **Gestió de fitxers externa als models** (eina `collab_files` list/read/write per a qualsevol model, arbre al context, avisos 🗂️ de canvis), API REST `/api/v1/collab`, i **panell UI 🤝** al canal (agents, selector visual de carpeta, guardarails, start/stop, arbre de fitxers amb visor) | Provat: unit del tool ✅, API 16/16 ✅, comandes 11/11 ✅, Svelte compila net. `channels.enable` activat a la BD de dev. Pendent: prova amb agents reals i extres Fase 4-5 |
| 2026-07-16 | fix + polish | **Bug crític arreglat**: les rondes es tancaven a l'instant — el backend anava amb `uvicorn --reload` i a Windows això trenca els subprocessos dels CLI (`NotImplementedError`). MAI arrencar amb `--reload`. També: errors de mà alçada visibles al canal, handraise one-shot (sense sessió), pipes sincronitzats a la BD automàticament, guardarails traduïts amb tooltip, i ruta de carpeta escrivible a mà | Verificat amb crida real al pipe (haiku → "ok") sense reload ✅. Detall al DEVLOG |
| 2026-07-16 | 0 + 1 + 4 + 5 — **PLA COMPLET** | Fase 4: tauler de tasques (eines + panell), consens explícit (`propose_finish` / `FEINA_ACABADA` + vot), resum incremental, descarts conscients (threads, Redis). Fase 5: stats 📊 i `max_round_seconds`, `COLLAB_ADMIN_ONLY`, errors visibles, README `docs/collab-workspace.md`, llista de 5 hooks. 2 fixos més: els pipes CLI descartaven el system prompt; l'emitter de canal deixava els missatges dels models buits (formats nous d'esdeveniment) | **Ronda real amb agents Claude verificada de cap a cap** (torn → fitxer → 🗂️ → proposta → vot → consens → 📊 → resum) en 22s ✅. Unit tool v2 ✅, REST 22/22 ✅ |
| 2026-07-16 | filosofia d'equip | **Primer planificar junts, després executar** (petició Esteve, inspirat en la seva nota de 2019): fases 📋 planificació → vot `PLA_ACORDAT` → 🔨 execució; bloc de filosofia (sense piràmide, submissió mútua, esperar dependències i anunciar) a tots els prompts; guardarail `require_planning`; `/collab phase`; xip de fase al panell; "⏳ treballant…" als torns en curs | Verificada la 1a taula real de l'Esteve (Codex+Haiku, web amb revisió creuada, 5 torns 8m52s). Detall al DEVLOG |
| 2026-07-16 | treball continu | **Fora el concepte de "rondes" que esperen un botó** (petició Esteve): `max_agent_turns` per defecte 0 (sense límit), empentes anti-silenci (si queda feina, torn forçat rotatiu amb avís del sistema; només després de 2 empentes sense reacció → 😴 repòs), llenguatge "sessió de treball / equip treballant / en repòs" | L'usuari només dona l'objectiu i intervé quan vol; ⏹ com a fre. Detall al DEVLOG |

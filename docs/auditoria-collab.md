# Pla de millora de l'espai col·laboratiu (taula rodona d'IAs)

> Consensuat per: Codex Sol (CLI), Claude Fable (CLI) i Z.ai.glm-5.2.
> Encàrrec de l'Esteve: pla EXHAUSTIU de millores (frontend i backend), consensuat per tots,
> amb **visibilitat del que fa cada IA** com a prioritat. Només pla — sense implementar.
> Estat: **Complet, W0–W15** (W11–W15 afegides el 17/07/2026 a petició de l'Esteve: perfils, personalització d'agents, modes de conversa, llegibilitat del xat i optimització de tokens). Pendent: validació de l'Esteve.
> 🔴 Crítica · 🟠 Alta · 🟡 Mitjana · 🟢 Baixa — Esforç: S/M/L
> Nota: versió condensada per límit d'escriptura d'un dels agents; les evidències detallades (fitxer:línia,
> esforç per troballa) són a l'historial de la conversa i es poden restaurar si l'Esteve vol el detall complet.

---

## 0. W0 — Recuperar i estabilitzar el runner de Codex (redactat per Codex Sol)

**Severitat:** 🔴 Crítica — **Prioritat:** immediata, abans de desenvolupar W1-W10, perquè la incapacitat
de Codex per inspeccionar o modificar el projecte redueix directament la capacitat de l'equip.

### Símptomes observats (dos bloquejos diferents, no confondre'ls)

1. **Fallada en crear el procés de PowerShell**
   - Error exacte: `CreateProcessAsUserW failed: 5 (Acceso denegado)`.
   - Va fallar repetidament, fins i tot amb operacions trivials com `Get-Location`.
   - La fallada succeeix **abans** d'executar l'ordre: no és un error del repositori ni de PowerShell intern,
     sinó de creació del procés sota la identitat assignada al runner.
   - Impacte: Codex no pot llegir, editar, executar proves ni obtenir diagnòstics locals.

2. **Perfil de permisos convertit en només lectura / política restrictiva**
   - El runner posterior indicava explícitament un filesystem restringit a lectura.
   - Una consulta **sense escriptura** sobre `docs/auditoria-collab.md` va ser rebutjada amb `blocked by policy`.
   - Executable implicat: `C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.3.0_x64__8wekyb3d8bbwe\pwsh.exe`.
   - Impacte: Codex pot raonar i redactar al xat, però no pot verificar l'estat compartit ni integrar les seves aportacions.

### Reproducció confirmada (evidència del 16 de juliol de 2026)

Verificació feta per Codex Sol amb un perfil que **anunciava `workspace-write`** i tot i així va fallar:

```
windows sandbox: runner failed during SpawnChild: CreateProcessAsUserW failed: 5 (Acceso denegado.)
```

- `cwd`: `D:\Proyectos\Personal\open-webui-esanpons`
- Executable: `C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.3.0_x64__8wekyb3d8bbwe\pwsh.exe`
- Arguments: `-NoProfile -Command ...`
- Fase: `SpawnChild` — **abans** d'executar `rg` o `Get-Content`
- Windows error: `5` · `si_flags=256` · `creation_flags=525312` · `env_u16_len=9112`

**Conclusió que canvia la diagnosi:** el bloqueig actual no el causa el contingut de l'ordre ni els permisos
del workspace — la creació del procés falla abans d'accedir a cap fitxer, tot i que el perfil declara
`workspace-write`. La hipòtesi principal passa a ser la identitat/token usat per `CreateProcessAsUserW`,
l'accés d'aquesta identitat al PowerShell empaquetat dins `WindowsApps`, o la combinació de flags de creació.

### Segona reproducció (evidència del 17 de juliol de 2026)

Mateix error `SpawnChild: CreateProcessAsUserW failed: 5`, però amb un executable **diferent**:
`C:\Users\EsteveSanponsCarball\AppData\Local\Microsoft\WindowsApps\pwsh.exe` (l'àlies App Execution de
l'usuari, no el paquet de `Program Files\WindowsApps`). Es mantenen `si_flags=256` i
`creation_flags=525312`. **Conseqüència:** es debilita la hipòtesi que el problema sigui exclusivament el
PowerShell empaquetat — la investigació ha de prioritzar el token/identitat del procés, els permisos sobre
els àlies App Execution i els flags de creació.

### Passos de diagnosi prioritaris (derivats de la reproducció)

1. Provar el mateix runner amb un `pwsh.exe` **no empaquetat** a `WindowsApps`.
2. Registrar la identitat/token abans de `SpawnChild`, sense exposar secrets.
3. Decodificar i comparar `creation_flags=525312` amb una execució funcional.
4. Comparar ACL i capacitat d'execució dels dos executables sota la mateixa identitat.
5. Verificar si el perfil d'usuari requerit per PowerShell existeix; `-NoProfile` no evita els permisos
   d'execució de l'AppX.
6. Afegir una **prova de salut prèvia al torn** que intenti crear un procés mínim i, si falla, classifiqui
   l'agent com `process_spawn_denied` (enllaça amb W1: estat visible a la barra d'agents).

### Diagnosi inicial (causes a investigar per separat)

Permisos de la identitat amb què el backend llança Codex; dret de Windows per crear processos amb
`CreateProcessAsUserW`; accés de la identitat al `pwsh.exe` empaquetat sota `WindowsApps`; token, sessió
interactiva i perfil d'usuari del procés; perfil de sandbox/permisos que l'orquestrador envia a Codex;
incoherència entre els permisos anunciats al prompt i els aplicats realment; regles que bloquegen ordres
de lectura per contingut, longitud o quoting; diferències entre execucions iniciades per la taula rodona
i una sessió Codex directa.

### Pla de diagnosi reproduïble

Executar la mateixa bateria des de: (1) sessió Codex directa, (2) pipe de Codex invocat manualment pel
backend, (3) torn de Codex dins la taula rodona. En cada entorn registrar: identitat efectiva, directori
de treball, executable i versió de PowerShell, perfil de permisos rebut, capacitat de crear un procés,
lectura d'un fitxer del projecte, creació/lectura/eliminació d'un fitxer temporal dins del workspace,
execució d'una ordre permesa no destructiva, codi de sortida, stderr i fase exacta on falla.

Proves mínimes: `Get-Location` · `whoami` · resolució de la ruta de `pwsh` · lectura de `README.md` ·
escriptura d'un fitxer temporal dins del workspace · execució d'una prova curta del projecte.
Sense secrets, tokens ni variables sensibles als logs.

### Correcció esperada

- El backend transmet a Codex el perfil de permisos correcte per a la tasca.
- La identitat del runner pot executar un shell estable i accedir al workspace compartit.
- Preferir una instal·lació de PowerShell executable per la identitat de servei; no dependre de
  `WindowsApps` si en restringeix l'accés.
- Un bloqueig de política retorna una causa estructurada: regla, operació, ruta i perfil aplicat.
- La UI diferencia `runner_unavailable`, `process_spawn_denied`, `read_only_workspace` i `command_blocked_by_policy`.
- Quan Codex perdi capacitats, la barra d'agents (W1) ho mostra i l'orquestrador reassigna només les tasques afectades.

### Criteris d'acceptació

- Codex completa 20 torns consecutius podent llegir el projecte.
- Pot crear i modificar un fitxer temporal dins del workspace quan la tasca ho autoritza.
- Pot executar proves no destructives.
- Funciona igual des d'una sessió directa i des de la taula rodona.
- Cap prova produeix `CreateProcessAsUserW failed: 5`.
- Una ordre de lectura vàlida no és rebutjada per la política.
- Els permisos efectius coincideixen amb els anunciats.
- Si hi ha una restricció intencionada, Codex i la UI reben una causa concreta i accionable.

### Relació amb la resta del pla

W0 va **abans de la Fase 0** (xarxa de proves). S'integra amb W1: l'estat de Codex ha de mostrar si està
operatiu, bloquejat per creació de procés, limitat a lectura o impedit per política.

---

## 1. Frontend i contracte d'esdeveniments (Claude Fable)

Fitxers: `CollabPanel.svelte`, `apis/collab/index.ts`, `Channel.svelte`, `orchestrator.py`.

| # | Problema | Sev | Proposta |
|---|---|---|---|
| F1 | **Sense estat observable per agent.** Placeholder `⏳ treballant…` i prou; durant hand-raising no es veu res. | 🔴 | Màquina d'estats per agent per socket `collab:agent_status` + **barra d'agents** al capçal del canal. |
| F2 | **"done" per polling** cada 1,5s; `turn_timeout=0` penja la ronda per sempre. | 🟠 | Senyal de finalització + timeout finit de seguretat. |
| F3 | **Panell per polling cada 7s**, no push. | 🟡 | Push via socket; polling només fallback. |
| F4 | **No es pot tallar un torn en curs** des de la UI. | 🟠 | `POST /turn/cancel` + botons «salta»/«talla». |
| F5 | **Errors fràgils**: `[object Object]`, status perdut, silencis. | 🟡 | Normalitzar errors (`{status, message}`). |
| F6 | **Accessibilitat**: botons només-emoji, modal sense focus-trap. | 🟡 | `aria-label`, `role="dialog"`, focus-trap, teclat. |
| F7 | **Agents caiguts només visibles al panell.** | 🟡 | Barra d'agents (F1). |
| F8 | **Sense senyal de vida** durant torns llargs. | 🟠 | `detail` actualitzat + cronòmetre al xip. |
| F9 | Cadenes **hardcoded en català**, fora d'i18next. | 🟢 | Passar per `$i18n.t(...)`. |
| F10 | **Sense indicació de fase/tasques** al canal amb panell tancat. | 🟢 | Barra d'agents amb fase + comptador. |

**Arquitectura objectiu:** store Svelte `collabState` alimentat per push; `CollabAgentsBar.svelte` al canal; detecció d'estats obsolets.

**1.4 UX de W9/W10 (peticions de l'Esteve):**
- **W9:** franja sota cada missatge de l'usuari: `Rebut per 3/3 · 2 valorant · 1 vol intervenir` (temps real via `collab_receipt`). Desplegable per agent. Si ningú respon: avís explícit.
- **W10:** feedback `⚡ missatge prioritari rebut`; cua de torns visible; interrupcions explicades.
- **Criteris UX:** confirmació ≤ 3s; missatge a mig procés reflectit abans del torn següent; mai silenci sense explicació; la mecànica de rondes invisible.

---

## 2. Backend, concurrència i recuperació (Codex Sol)

Fitxers: `orchestrator.py`, `router.py`, `config.py`, `tasks.py`, `history.py`.

| # | Problema | Sev | Proposta |
|---|---|---|---|
| B1 | **Timeout no cancel·la la generació.** | 🔴 | `run_id/turn_id` cancel·lables; propagar cancel·lació. |
| B2 | **Exclusió de rondes només en memòria.** | 🔴 | Lease persistent per canal amb compare-and-set. |
| B3 | **Aturada només cooperativa entre torns.** | 🟠 | Cancel·lació per `run_id/turn_id`, `asyncio.Event`. |
| B4 | **Recuperació post-reinici incompleta.** | 🟠 | Persistir sessió/torn; reconciliar orfes. |
| B5 | **Salut d'agent heurística.** | 🟠 | Resultat tipat + circuit breaker persistent. |
| B6 | **Sense backpressure global.** | 🟠 | Semàfors globals i per proveïdor. |
| B7 | **No hi ha contracte d'esdeveniments.** | 🔴 | Envelope `collab_event.v1` amb `seq` i re-sync. |
| B8 | **Missatge humà espera la ronda completa.** | 🟠 | Unificar entrada en gestor de jobs amb lease. |
| B9 | **`run_round` massa concentrat.** | 🟡 | Extreure `TurnRunner`, `AgentHealth`, `SpeakerPolicy`, `CollabEventPublisher`. |

**2.4 Scheduler continu (W10):** `esdeveniment persistent → avaluació paral·lela → cua prioritzada → torn supervisat → nou esdeveniment`. Migració incremental (4 passos: taules → lease → preempció → event loop). Política d'interrupció: `queued (immediata) → generating (cancel·lar) → streaming (aturar si possible) → tool/file (acabar unitat atòmica)`. Mai iniciar torn nou sense confirmar terminal de l'anterior.

**Criteris d'acceptació backend (W10):** un missatge humà rebut durant activitat genera un event persistent i invalida la cua anterior en pocs segons; cap `handraise` calculat amb context obsolet pot iniciar un torn; reinicis i múltiples workers mantenen un únic consumidor efectiu per canal; la invalidació i el reprocessament són idempotents; les operacions amb efectes no queden parcialment executades per una interrupció; la ronda deixa de ser una barrera observable.

---

## 3. Seguretat, persistència, proves i model de dades (Z.ai.glm-5.2)

Fitxers: `tasks.py`, `config.py`, `files.py`, `file_tools.py`, `history.py`, `commands.py`, `router.py`, `orchestrator.py`.

### 3.1 Troballes

| # | Problema | Sev | Proposta |
|---|---|---|---|
| S1 | **Race read-modify-write sobre `channel.meta`.** `set_meta_key`/`save_collab_config` llegeixen, modifiquen i escriuen el JSON sencer sense `FOR UPDATE`. Possible en ronda: tasques perdudes silenciosament. | 🔴 | `SELECT … FOR UPDATE`; migrar a taules pròpies (S8); versionatge optimistic per config. |
| S2 | **Escriptura no atòmica**; last-write-wins silenciós. | 🟠 | `tmp + os.replace()`; detecció `mtime`. |
| S3 | **`config.agents` no valida models.** | 🟡 | Validar contra `get_all_models()`. |
| S4 | **(FORT) Path traversal controlat** (`resolve_safe`). | 🟢 | Tests de regressió. |
| S5 | **Zero tests de `collab/`.** | 🔴 | Fase 0: tests de funcions pures + DB mockada. |
| S6 | **LIKE sense escapar comodins.** | 🟢 | Escapar `%` i `_`. |
| S7 | **Sense límit de mida** a `write_text_file`. | 🟡 | Aplicar `MAX_FILE_BYTES`. |
| S8 | **Estat barrejat en `channel.meta`.** | 🟠 | Taules pròpies; `channel.meta` només `CollabConfig`. |

### 3.4 Model de dades del scheduler (W9 + W10)

Les tres taules neixen separades de `channel.meta` des de la Fase 1. Cap read-modify-write sobre JSON. Seqüència amb `BEGIN IMMEDIATE` a SQLite. Tanca S1 estructuralment.

**`collab_session`** — lifecycle + lease per canal:
`channel_id` PK · `status` (active/idle/stopped) · `lease_owner` · `lease_expires_at` · `last_event_seq` · `updated_at`
```sql
UPDATE collab_session SET lease_owner=:wid, lease_expires_at=:now+30s
WHERE channel_id=:cid AND (lease_owner=:wid OR lease_expires_at<:now);
```
0 files = un altre worker té el lease. Renovació cada 10s. TTL expira → altre worker recull. `last_event_seq` permet reprendre.

**`collab_event`** — log append-only ordenat per `seq`:
`id` PK · `channel_id` FK · `seq` (monòton per canal) · `type` (user_message/agent_message/handraise/turn_start/turn_end/agent_state) · `agent_id` · `message_id` · `payload` JSON · `status` (active/superseded/consumed) · `created_at`
```sql
BEGIN IMMEDIATE;
INSERT INTO collab_event VALUES (:id,:cid,
  (SELECT COALESCE(MAX(seq),0)+1 FROM collab_event WHERE channel_id=:cid),
  :type,:agent_id,:message_id,:payload,'active',:now);
COMMIT;
```
Constraint `UNIQUE(channel_id, seq)` addicional.

**`collab_receipt`** — traçabilitat per agent (W9):
`id` PK · `event_seq` FK · `channel_id` FK · `agent_id` · `state` (received→incorporated→evaluating→will_intervene/pass) · `message_id` · `updated_at`
Constraint `UNIQUE(event_seq, agent_id)`. Cada transició s'emet per socket `collab:receipt_updated`. Resum `Rebut per X/N · Y valorant · Z vol intervenir` = agregació simple.

**Invalidació idempotent:**
```sql
UPDATE collab_event SET status='superseded'
WHERE channel_id=:cid AND seq<:N AND type='handraise' AND status='active';
```
Idempotent: `superseded` és terminal. El scheduler sempre processa el `user_message` més recent.

**Per què no reintrodueix S1:** no hi ha JSON global (escriptura per fila directa); cada agent té la seva fila a `collab_receipt`; l'event log és append-only; `channel.meta` només té `CollabConfig`; `last_event_seq` i `lease_owner` persisteixen.

**Criteris (W9+W10):** seq monòtona sense buits amb concurrència; un sol lease holder; TTL → un altre worker reprocessa; invalidació idempotent; un `collab_receipt` per agent per missatge; cap read-modify-write sobre `channel.meta`.

---

## 4. Perfils, personalització, modes i tokens (W11–W15) — peticions de l'Esteve del 17/07/2026

> Redacció consensuada: arquitectura i diagnosi de consum de **Codex Sol**, model de dades i política de
> pressupostos de **Z.ai.glm-5.2**, UX de **Claude Fable**. Integrat per Claude Fable (Codex sense escriptura
> per W0 pendent; Z.ai caigut en el moment de la integració — vegeu l'evidència a §4.5).

### 4.1 W11 — Perfils reutilitzables de taula rodona 🟠

Crear, editar, duplicar, versionar, exportar i seleccionar perfils. Un perfil agrupa agents, mode de
conversa, prompts, límits, eines i configuració visual. En crear una taula es comença des d'un perfil i
els ajustos posteriors són locals, sense modificar l'original.

**Problema actual (evidència):** tota la configuració viu a `channel.meta['collab']` com un `CollabConfig`
(config.py:48-57). `config.agents` és `list[str]` — només IDs de model (CollabPanel.svelte:216), sense rol,
prompt, effort ni color. No hi ha cap concepte de plantilla: cada taula es configura des de zero. Afegir
perfils com a més JSON dins `channel.meta` reintroduiria S1.

**Model de dades — taula pròpia `collab_profile`** (Z.ai.glm-5.2):

| Columna | Tipus | Propòsit |
|---|---|---|
| `id` | Text PK | UUID |
| `user_id` | Text FK → user | Propietari |
| `name` / `description` | Text | Nom visible / descripció opcional |
| `config` | JSON | `CollabConfig` serialitzat (agents, mode, guardrails, project_dir) |
| `agent_overrides` | JSON | Llista de `{model_id, role, prompt, effort, token_limit, tools, priority, color, avatar}` |
| `budget` | JSON nullable | Pressupostos (W15) — null = sense límit |
| `is_template` | Boolean | Perfil públic de sistema, heretable per tothom |
| `created_at` / `updated_at` | BigInteger | |

```sql
CREATE TABLE collab_profile (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT,
  config JSON NOT NULL DEFAULT '{}', agent_overrides JSON NOT NULL DEFAULT '[]',
  budget JSON, is_template BOOLEAN NOT NULL DEFAULT 0,
  updated_at BIGINT NOT NULL, created_at BIGINT NOT NULL
);
CREATE INDEX idx_collab_profile_user ON collab_profile(user_id);
```

**Configuració efectiva del canal — taula `collab_channel_config`** (correcció de Codex Sol, 17/07/2026):
guardar `agent_overrides` i `budget` a `channel.meta`, encara que fossin claus separades, els faria
participar físicament del mateix read-modify-write del JSON i reintroduiria S1. La còpia efectiva viu en
taula pròpia:

`channel_id` PK · `source_profile_id` · `source_profile_version` · `config` JSON ·
`agent_overrides` JSON · `budget` JSON · `version` (actualització optimista) · `updated_at`

El perfil és la plantilla; `collab_channel_config` és la configuració efectiva i independent del canal.
A més permet saber amb quin perfil i versió va començar cada sessió.

**Cicle de vida:** `POST /collab/profiles/:id/apply` copia el perfil a `collab_channel_config` (el perfil
original no es toca; els canvis posteriors només muten la fila del canal, amb `version` optimista).
`POST /collab/profiles` desa la config efectiva del canal com a perfil nou. `GET .../export` /
`POST .../import` (JSON autocontingut) i `POST .../duplicate`.

**Per què `agent_overrides` no va dins `config`:** mantenir `CollabConfig` intacte evita trencar
l'orquestrador; els overrides són capa opcional (amb override s'usa, sense override el comportament és
l'actual). Els perfils s'implementen sense tocar `CollabConfig` ni `run_round`.

**UX (Claude Fable):** pantalla pròpia a Espai de treball (com Models/Prompts/Eines) amb llista, crear,
duplicar, editar, exportar/importar. En crear una taula rodona: primer pas «Tria un perfil» (o «Començar en
blanc»); botó «Desa com a perfil nou» al panell del canal per al camí invers.

**Criteris d'acceptació:** CRUD complet de perfils; aplicar un perfil → el canal hereta la config i les
modificacions posteriors no toquen el perfil; export/import JSON autocontingut; overrides visibles a la UI
(targeta W12); perfils predefinits `is_template=true` per a tothom; `agent_overrides` només pot referenciar
`model_id`s presents a `config.agents`.

### 4.2 W12 — Configuració individual dels agents 🟠

Per cada agent: model, rol, prompt personalitzat, `effort`, límit de tokens, eines/permisos, prioritat
d'intervenció i condicions per participar o passar. **Validar quins proveïdors admeten realment `effort`;
no enviar-lo cegament a tots** (Codex Sol).

**UX — targeta expandible per agent (Claude Fable):** la fila actual (nom + 🔻 + ✕) passa a targeta amb:
rol/nom mostrat, prompt personalitzat, effort (camp desactivat i explicat si el connector no el suporta),
límit de tokens propi, i color/avatar (alimenta W14). Comparteix model de dades amb W11 (`agent_overrides`):
separar les línies és correcte per UX (perfils = reutilitzar; W12 = personalitzar), però la implementació
és un sol bloc.

### 4.3 W13 — Modes de conversa configurables 🟠

**Relació amb W10 (aclariment de l'Esteve, 17/07/2026):** W10 i W13 no són el mateix. **W10 construeix
el nou mode de conversa fluida** (sense rondes rígides: els agents intervenen quan correspon i els
missatges de l'usuari tenen prioritat); **W13 és només la configuració** que permet triar entre el mode
tradicional per rondes i el mode fluid de W10, més la política de qui parla. W13 no implementa cap mode:
exposa els que existeixen.

**Dos eixos independents, no un sol selector** (correcció de Codex Sol, 17/07/2026): el selector
`handraise`/`roundrobin` existent (CollabPanel.svelte:605-606) decideix *qui parla*; quan es reavalua la
conversa és un altre eix. La configuració queda:

- **`conversation_mode`**: `rounds` (comportament actual — deliberacions formals, votacions, control
  estricte de cost) | `continuous` (l'arquitectura W10: preempció, re-avaluació per esdeveniment).
- **`speaker_policy`**: `handraise` | `round_robin` (el selector actual, intacte).
- Un eventual mode **híbrid** s'ha de definir com una combinació concreta d'aquests dos eixos (p. ex.
  `continuous` + finestres de votació en `rounds`), mai com un tercer valor ambigu.

Això fa configurable «evitar els torns», tal com demana l'Esteve, sense perdre el mode formal quan convé.

### 4.4 W14 — Identitat visual i llegibilitat del xat 🟠

**Restricció de l'Esteve (17/07/2026): cap pèrdua d'informació.** No s'agrupen, pleguen, oculten ni
filtren missatges — tots els missatges i esdeveniments queden visibles, individuals i en ordre. W14 només
millora la *presentació* del que ja es veu:

- **Color d'accent per agent** (vora esquerra del missatge + fons del nom), assignat automàticament d'una
  paleta accessible, personalitzable a la targeta W12; **avatar/logotip o inicials** al costat del nom;
  **rol sota el nom** en gris petit.
- **Diferenciació visual dels avisos tècnics:** esdeveniments de sistema (🗳️ votacions, 🗂️ fitxers tocats,
  estats W1) amb estil propi (fons, tipografia, icona) que els distingeixi de la conversa — sempre visibles
  i al seu lloc, mai plegats ni compactats.
- **Espaiat i jerarquia tipogràfica:** més aire entre intervencions d'agents diferents, capçalera clara a
  cada missatge (cada missatge conserva la seva).
- **Accessibilitat:** el color mai és l'únic senyal (sempre nom + avatar) — Codex Sol.
- **Millores de xat opcionals** (l'Esteve tria; cap amaga contingut): (a) *fil de resposta* — cada missatge
  indica a qui respon amb enllaç; (b) *resum lateral* — el resum de secretari sempre visible en un panell;
  (c) *salt a la meva última intervenció*.

*(Descartat per decisió de l'Esteve: agrupació de missatges consecutius, línies d'activitat plegables i
filtres per agent/tipus — proposats inicialment, retirats el 17/07/2026 per no perdre informació.)*

### 4.5 W15 — Pressupost i optimització de context/tokens 🔴

**Evidència en directe (17 de juliol de 2026):** Z.ai.glm-5.2 va caure amb «error del model (possible
límit de quota)» immediatament després de lliurar la seva proposta tècnica en aquesta mateixa sessió. És el
fenomen exacte que preocupa l'Esteve. Avui el sistema només pot dir «possible límit de quota»: la detecció
és heurística (`_ERROR_CONTENT_RE`, orchestrator.py:30) i `_mark_agent_down` (línia 56) registra el motiu
com a text lliure. **No podem afirmar que els models gratuïts caiguessin només per ingesta de tokens**
(pot ser quota, timeout, límit de context o error del proveïdor) — cal telemetria per distingir-ho.

**Problema actual (evidència, Z.ai.glm-5.2):** `build_transcript` (orchestrator.py:116-141) reenvia els
últims N missatges sencers (per defecte 30) a **cada** agent a **cada** hand-raising i a **cada** torn; el
`system` hi afegeix filosofia (~1500 tokens) + `_project_block` (amb arbre de fitxers!) + `_board_text`.
Desproporcionat per a un simple «vols intervenir?». A més: crides de votació i resum sense comptador ni
pressupost.

**Proposta en tres capes:**

**Capa 1 — Telemetria (Fase 0: mesurar abans de limitar).** Taula `collab_usage`, un registre per crida:
`id` PK · `channel_id` · `agent_id` · `call_type` (handraise/turn/vote/summary) · `input_tokens` ·
`output_tokens` · `total_tokens` · `estimated_cost` · `status` · `error_detail` · `created_at`, amb índexs
per `(channel_id, created_at)` i `(agent_id, created_at)`. **`error_detail` sanejat i limitat** (Codex Sol):
pot contenir fragments retornats pel proveïdor — mai prompts, tokens secrets ni respostes completes.

Classificació d'errors (substitueix `_ERROR_CONTENT_RE`):

| Error crú | `status` | Acció |
|---|---|---|
| HTTP 429 / rate_limit / quota | `quota_exceeded` | Agent caigut, no reintentar fins cooldown |
| HTTP 413 / context_length_exceeded | `context_too_large` | Reduir `context_messages` automàticament |
| Timeout asyncio | `timeout` | Comportament actual |
| Connection refused / 500 | `provider_error` | Circuit breaker persistent (B5) |
| Resposta buida | `empty_response` | Comportament actual |
| Error del CLI | `cli_error` | Patrons `_ERROR_CONTENT_RE` existents |

**Capa 2 — Pressupostos (limitar un cop mesurat).** Al `budget` del perfil (W11):
`session_total_tokens`, `session_total_cost`, `per_agent_tokens`, `per_turn_tokens`,
`per_handraise_tokens`, `degradation_threshold` (0.8 per defecte), `action_on_exhaustion`
(pause/downgrade/stop). Al 80%: reducció de context automàtica (Capa 3); al 100%: pausa amb avís, degradació
a model més barat o aturada. El backend consulta el pressupost abans de cada crida i l'actualitza amb el
`usage` real de la resposta de l'API.

**Agregat en calent `collab_budget_tracker`** (Z.ai.glm-5.2, 17/07/2026): la consulta de pressupost no pot
ser un `SUM(total_tokens)` sobre `collab_usage` a cada crida (lent en sessions llargues). Taula agregada:

```sql
CREATE TABLE collab_budget_tracker (
  channel_id TEXT NOT NULL, agent_id TEXT NOT NULL,
  consumed_tokens INTEGER NOT NULL DEFAULT 0,
  consumed_cost   REAL    NOT NULL DEFAULT 0.0,
  call_count      INTEGER NOT NULL DEFAULT 0,
  updated_at BIGINT NOT NULL,
  PRIMARY KEY (channel_id, agent_id)
);
```

Actualitzada atòmicament dins la mateixa transacció que la inserció al log (aquí sí cal `BEGIN IMMEDIATE`:
log + agregat han de ser consistents sota concurrència): `INSERT INTO collab_usage ...` +
`INSERT INTO collab_budget_tracker ... ON CONFLICT(channel_id, agent_id) DO UPDATE SET
consumed_tokens = consumed_tokens + :total_tok, consumed_cost = consumed_cost + :cost,
call_count = call_count + 1`. La consulta de pressupost queda O(1):
`SELECT consumed_tokens FROM collab_budget_tracker WHERE channel_id=:cid AND agent_id=:aid`.

**Capa 3 — Reducció automàtica de context (la mesura més impactant):**
1. **Hand-raise lleuger:** només els últims 5 missatges + el resum incremental (ja existeix,
   `_update_summary`); arbre de fitxers i filosofia completa només al torn actiu. Estalvi estimat 60-70%
   per hand-raise. **Criteri de no-regressió:** els agents han de seguir encertant quan volen intervenir;
   si retallar context degrada les decisions, es recalibra abans de consolidar-ho.
2. **Context adaptatiu:** agent amb dos `pass` seguits → el proper hand-raise només rep el delta des de la
   seva última avaluació (`last_evaluated_seq` per agent).
3. **Deduplicació d'instruccions:** prompt caching del system (si el proveïdor ho suporta) o versió
   essencial de la filosofia per als hand-raises.
4. **Comptadors visibles (UX):** consum per agent a la barra (`45.2k / 100k tokens`), cost estimat de la
   sessió al peu del panell — l'Esteve veu el consum mentre passa, no quan un model mor.

**Criteris d'acceptació:** cada crida registra tokens, `status` i `error_detail` a `collab_usage`; errors
classificats en 6 categories (no text lliure); la comprovació de pressupost consulta `collab_budget_tracker`
en O(1) (mai `SUM` sobre el log) i log+agregat s'actualitzen en la mateixa transacció; un pressupost
exhaurit atura o degrada amb avís visible;
hand-raise lleuger redueix ≥50% el consum sense regressió de decisions; consum per agent i cost de sessió
visibles en temps real; la telemetria pot respondre amb dades reals per què va caure cada model gratuït.

---

## 5. Pla conjunt

### 5.1 Diagnosi prioritzada (16 línies)

| Línia | Troballes | Sev | Què resol |
|---|---|---|---|
| **W0 — Runner de Codex operatiu** *(Esteve)* | §0: `CreateProcessAsUserW failed: 5` + política només-lectura | 🔴 | Codex pot tornar a llegir, escriure i provar — l'equip recupera un terç de la seva capacitat. |
| **W1 — Visibilitat d'agents** *(Esteve)* | F1+B7, F8, F7, F10 | 🔴 | Saber qui escolta, pensa, executa, espera, ha caigut. |
| **W2 — Cancel·lació i timeout reals** | B1+F2, B3+F4 | 🔴 | Tallar agent penjat a l'instant; cap ronda penjada. |
| **W3 — Una sola ronda, recuperable** | B2, B4, B8 | 🔴 | Mai dues rondes; estat coherent post-reinici. |
| **W4 — Persistència i fitxers** | S1, S8, S2, S7 | 🔴 | Dos agents no es perden feina; cap fitxer corrupte. |
| **W5 — Salut i càrrega** | B5, B6, S3 | 🟠 | Diagnòstic d'errors; límits sans. |
| **W6 — Qualitat UX** | F3, F5, F6 | 🟡 | Panell reactiu, errors llegibles, teclat. |
| **W7 — Mantenibilitat** | B9, F9, S8 | 🟡 | Evolucionar amb confiança. |
| **W8 — Seguretat menor** | S4, S6 | 🟢 | Mantenir garanties. |
| **W9 — El missatge humà mai cau al buit** *(Esteve)* | `collab_receipt` (§1.4, §2.4, §3.4) | 🔴 | Saber que el teu missatge ha estat rebut, valorat i qui respondrà. |
| **W10 — De rondes a conversa contínua** *(Esteve)* | Scheduler amb lease; preempció; invalidació idempotent; política escalonada | 🔴 | Escoltar, valorar i reordenar sense esperar cap ronda. |
| **W11 — Perfils reutilitzables** *(Esteve)* | §4.1: taula `collab_profile`, aplicar-com-a-còpia, export/import | 🟠 | Configurar una vegada, reutilitzar sempre. |
| **W12 — Configuració individual d'agents** *(Esteve)* | §4.2: `agent_overrides` (rol, prompt, effort, límits, color) | 🟠 | Cada agent afinat a la seva funció. |
| **W13 — Modes de conversa configurables** *(Esteve)* | §4.3: dos eixos — `conversation_mode` (rondes/continu de W10) + `speaker_policy`; W13 només configura, no implementa cap mode | 🟠 | Triar entre control formal i fluïdesa (W10). |
| **W14 — Identitat visual i llegibilitat** *(Esteve)* | §4.4: colors + avatars + rols + avisos tècnics diferenciats — sense agrupar ni ocultar res | 🟠 | Seguir la conversa sense perdre's en el text ni perdre informació. |
| **W15 — Pressupost i optimització de tokens** *(Esteve)* | §4.5: `collab_usage`, 6 categories d'error, pressupostos, hand-raise lleuger | 🔴 | Saber què consumeix cada agent i per què cau; que els models gratuïts puguin seguir a la taula. |

### 5.2 Roadmap

Ordre acordat per l'equip (Codex Sol): **W0 → instrumentació W15 → W1/W9/W10 → W11–W14**. Sense recuperar
Codex ni mesurar el consum real, la resta s'implementaria amb menys capacitat d'equip i sense saber on es
perden els tokens.

- **Fase W0** (immediata, abans de tot): diagnosi reproduïble i correcció del runner de Codex (§0) — desbloqueja la participació activa de Codex a totes les fases següents.
- **Fase 0** (S-M): tests bàsics de `collab/` (S5, B9) + **telemetria W15 Capa 1** (`collab_usage` + classificació d'errors) — sense telemetria no sabem on optimitzar ni per què cauen els models.
- **Fase 1** (L): taules `collab_session`/`collab_event`/`collab_receipt` (§3.4); contracte `collab_event.v1` (B7); màquina d'estats per agent (F1+F8); preempció del missatge humà sobre `run_round` (§2.4 passos 1-3); traçabilitat W9 (§1.4); `CollabAgentsBar.svelte` (F7+F10); **W15 Capa 3** (hand-raise lleuger, aprofita les taules del scheduler).
- **Fase 2** (L): interrupció segura (W2 + política escalonada W10); botons «salta/talla» (F4); timeout no desactivable; **W15 Capa 2** (pressupostos actius amb degradació).
- **Fase 3** (L): separació de dades completa (S1/S8); recuperació post-reinici (B2/B4); escriptura atòmica (S2/S7); **W11 + W12** (perfils i overrides — un sol bloc d'implementació).
- **Fase 4** (M): salut tipada (B5); backpressure (B6); validació d'agents (S3); **W13** (modes de conversa) + **W14** (identitat visual).
- **Fase 5** (M): push (F3); errors (F5); accessibilitat (F6); i18n (F9); extraccions (B9); LIKE (S6).
- **Fase 6** (L): event loop estructural (W10 complet). La ronda desapareix com a concepte intern.

### 5.3 Matriu de proves

| W | Unitaris | Integració | Manual |
|---|---|---|---|
| **W0** | Classificació d'errors: cada tipus (`runner_unavailable`, `process_spawn_denied`, `read_only_workspace`, `command_blocked_by_policy`) es produeix a partir de l'error crú i conté `regla, operació, ruta, perfil`; la prova de salut prèvia al torn (punt 6 §0) classifica correctament un procés que no pot spawnar com a `process_spawn_denied`. | Bateria §0 executada als 3 entorns (sessió directa, pipe backend, taula rodona) amb resultats idèntics; permisos efectius = anunciats; una ordre de lectura vàlida no és rebutjada per la política; una restricció intencionada retorna causa estructurada amb regla+operació+ruta+perfil; Codex pot executar proves no destructives (`pytest -k smoke`). | **Nou**: 20 torns consecutius de Codex a la taula rodona llegint i escrivint el projecte sense cap `CreateProcessAsUserW failed: 5` ni `blocked by policy`. |
| **W1** | Màquina d'estats; `collab_event.v1` (seq, re-sync). | Socket rep `collab:agent_status`; frontend reconstrueix post-desconnexió. | §1+§12; **nou**: barra d'agents en temps real. |
| **W2** | Propagació cancel·lació; timeout atura generació. | `POST /turn/cancel` deixa de generar. | §7; **nou**: «✖ talla» amb agent lent. |
| **W3** | Lease compare-and-set; reconciliació d'orfes. | Dos inicis = una ronda; post-reinici coherent. | **Nou**: reiniciar a mig ronda; veure «interromput». |
| **W4** | `set_meta_key` concurrència; `os.replace()` post-fallada; `mtime`. | Dos agents mateix fitxer = error conflicte. | §4+§5; **nou**: dos agents mateix fitxer. |
| **W5** | Circuit breaker; semàfors backpressure. | Càrrega respecta límits. | §11; **nou**: quota exhaurida diagnosticada. |
| **W6** | Normalització errors; estat d'error. | Push config/tasques; focus-trap. | §6+§8; **nou**: teclat + errors. |
| **W7** | Mòduls extrets provables; funcions pures. | Refactor no trenca tests. | Checklist completa. |
| **W8** | `resolve_safe` extremals; escapament LIKE. | (No cal.) | **Nou**: accés fora projecte bloquejat. |
| **W9** | `UNIQUE(event_seq,agent_id)`: duplicat falla; ordre de transicions; càlcul resum X/N. | UI rep `collab:receipt_updated` i actualitza resum en temps real; missatge a mig ronda genera receipt per agent abans del torn següent; ningú `will_intervene` = avís. | **Nou**: missatge durant ronda → resum canvia «0/3»→«3/3 · 2 valorant · 1 vol intervenir»; no queda enterrat. |
| **W10** | Seq amb `BEGIN IMMEDIATE`: 50 insercions concurrents = seq 1-50 sense buit; lease: dos workers, un guanya; invalidació `superseded` idempotent (executar 10 vegades = mateix resultat). | Dos workers competeixen: un processa, l'altre insereix i surt; lease holder mor → TTL expira → altre recull i reprocessa des de `last_event_seq`; missatge humà a mig torn invalida handraises i força reavaluació abans del torn següent. | **Nou**: escriure durant torn actiu → veure «⚡ prioritari rebut» + cua reavaluada + handraises antics marcats com a superseded; el torn actiu acaba operació segura i cedeix. |
| **W11** | Validació d'`agent_overrides` (només `model_id`s de `config.agents`); export→import = perfil idèntic; aplicar perfil no muta l'original; actualització optimista de `collab_channel_config` (dos writes amb la mateixa `version` → un falla, cap perdut silenciosament). | `POST .../apply` copia el perfil a `collab_channel_config` (amb `source_profile_id`+`version`); modificar el canal després no toca el perfil; «Desa com a perfil nou» captura l'estat efectiu del canal; perfils `is_template` visibles per a tots els usuaris. | **Nou**: crear taula des de perfil, ajustar-la, desar-la com a perfil nou, exportar i reimportar. |
| **W12** | Merge override+config per agent (amb override s'usa; sense, comportament actual); `effort` no s'envia a proveïdors que no el suporten. | Torn d'agent amb prompt/rol personalitzat el rep al system; camp effort desactivat a la UI si el connector no el declara. | **Nou**: dos agents amb rols i prompts diferents es comporten diferent a la mateixa taula. |
| **W13** | Selecció de mode persisteix; mode `rondes` conserva el comportament actual (regressió). | Mode `continu` activa la preempció W10; canviar de mode a mig sessió aplica al següent cicle sense corrompre estat. | **Nou**: la mateixa taula en mode `rondes` vs `continu` — l'Esteve nota la diferència de fluïdesa. |
| **W14** | Assignació de colors de paleta accessible estable per agent; estil diferenciat dels avisos tècnics. | Cap missatge ni esdeveniment desapareix ni es compacta amb la nova presentació: el recompte i l'ordre del DOM coincideixen amb l'anterior (no-pèrdua d'informació). | **Nou**: conversa llarga amb 3 agents — es distingeix qui parla d'un cop d'ull; contrast validat; tota la informació segueix visible i en ordre. |
| **W15** | Classificació dels 6 errors a partir d'errors crus; càlcul de pressupost (80%/100%); hand-raise lleuger construeix el context reduït correcte. | Cada crida escriu a `collab_usage` amb `usage` real de l'API; pressupost exhaurit → pausa/degradació amb avís; HTTP 429 simulat → `quota_exceeded` (no text lliure); comptadors de la UI = suma de `collab_usage`. | **Nou**: sessió amb pressupost baix → veure comptadors pujar, avís al 80% i pausa al 100%; **no-regressió del hand-raise lleuger**: mateixa bateria de converses, les decisions d'intervenir no empitjoren. |

> Nota: els escenaris marcats **nou** no existeixen a `docs/collab-proves.md`; afegir-los forma part del lliurable de cada fase.

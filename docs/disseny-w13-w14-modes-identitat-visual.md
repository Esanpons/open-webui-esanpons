# Disseny W13/W14 — Modes configurables i identitat visual

> Autor: Z.ai.glm-5.2 · 18/07/2026
> Estat: disseny complet, pendent de revisió d'equip.
> Relacionat: `docs/auditoria-collab.md` §W13 i §W14.

---

## Resum

| Bloc | Què resol | Estat actual | Esforç |
|---|---|---|---|
| **W13 — Modes configurables** | Tancar el contracte entre perfils (W11) i els modes de conversa (`roundrobin`/`continuous`/`handraise`). Fer que els modes estiguin definits als perfils, no a la config ad hoc. | `roundrobin`/`continuous` + selector frontend persistents. No hi ha integració amb perfils W11/W12. | M |
| **W14 — Identitat visual** | Cada agent té color, avatar i rol propis a la UI (definits als overrides W12). Els estats són llegibles amb contrast suficient. | Barra i estats diferencien agents per nom; sense colors/avatars/rols personalitzats via overrides. | M |

---

## W13 — Modes configurables

### Problema

Avui els modes de conversa (`mode = "roundrobin" | "handraise"`, `conversation_mode = "rounds" | "continuous"`) estan definits al `CollabConfig` (ad hoc per canal). Quan es va dissenyar W11/W12, els perfils van quedar com a plantilles separades. Falta:

1. **Contracte unificat:** els modes haurien de viure als perfils, no a la config ad hoc, perquè aplicar un perfil canviï tot el comportament.
2. **Presets de mode:** plantilles predefinides ("debate", "standup", "code-review") que un usuari pot aplicar amb un clic.
3. **Permisos:** qui pot canviar el mode (admin vs editor vs lector).
4. **Proves:** no hi ha tests que verifiquin que aplicar un perfil canvia els modes correctament.

### Arquitectura

#### 13.1 Modes al perfil

El `CollabProfile` (ja existeix a `profiles.py`) ha d'incloure els camps de mode:

```python
class CollabProfile(Base):
    # ... camps existents ...
    mode: Mapped[str] = mapped_column(String(32), default="handraise")  # "handraise" | "roundrobin"
    conversation_mode: Mapped[str] = mapped_column(String(32), default="continuous")  # "continuous" | "rounds"
    guardrails: Mapped[dict] = mapped_column(JSON, default=dict)  # heretats de config
```

Quan s'aplica un perfil amb `apply_profile()`, aquests camps es copien a `collab_channel_config`:

```python
channel_config["mode"] = profile.mode
channel_config["conversation_mode"] = profile.conversation_mode
channel_config["guardrails"] = profile.guardrails
```

I l'orquestrador llegeix `collab_channel_config` abans de cada ronda:

```python
config = get_collab_config(fresh_channel)
channel_config = await get_channel_config(channel.id)
if channel_config:
    config.mode = channel_config.get("mode", config.mode)
    config.conversation_mode = channel_config.get("conversation_mode", config.conversation_mode)
```

**Retrocompatibilitat:** si no hi ha `collab_channel_config` (canal legacy), el comportament no canvia — usa `config.mode` i `config.conversation_mode` directament.

#### 13.2 Presets de mode

Plantilles predefinides que l'usuari pot aplicar amb un clic des del panell:

| Preset | `mode` | `conversation_mode` | `guardrails` | Ús |
|---|---|---|---|---|
| **Debate** | `handraise` | `continuous` | `max_agent_turns: 0, context_messages: 30, allow_self_reply: true` | Agents debaten fins consens |
| **Standup** | `roundrobin` | `rounds` | `max_agent_turns: 3, context_messages: 15` | Una passada per agent |
| **Code review** | `handraise` | `continuous` | `max_agent_turns: 20, context_messages: 40, require_planning: false` | Revisió de codi llarga |
| **Quick help** | `handraise` | `rounds` | `max_agent_turns: 5, context_messages: 10, require_planning: false` | Q&A ràpida |

Aquests presets es poden emmagatzemar com a `CollabProfile` amb `is_template=True` (camp que ja existeix al model). Es creen via migració de seed data.

#### 13.3 Permisos

| Rol | Pot canviar mode? | Pot aplicar perfil? | Pot editar perfils? |
|---|---|---|---|
| Admin (owner del canal) | ✅ | ✅ | ✅ |
| Editor | ✅ | ✅ | ❌ (només els seus) |
| Lector | ❌ | ❌ | ❌ |

El `_check_can_manage` ja existeix al router i valida l'accés. Només cal aplicar-lo a tots els endpoints relacionats amb modes i perfils.

#### 13.4 Migració del mode

Quan un canal te `channel.meta` amb `mode` i `conversation_mode` legacy:

```python
# A ensure_channel_config() — lazy migration
legacy_mode = channel.meta.get("collab_mode", "handraise")
legacy_conv = channel.meta.get("collab_conversation_mode", "continuous")
if "mode" not in channel_config:
    channel_config["mode"] = legacy_mode
if "conversation_mode" not in channel_config:
    channel_config["conversation_mode"] = legacy_conv
```

### Criteris d'acceptació

1. Els modes (`mode` + `conversation_mode`) es defineixen als perfils i es copien a `collab_channel_config` quan s'aplica un perfil.
2. Hi ha almenys 4 presets (`debate`, `standup`, `code_review`, `quick_help`) disponibles a la UI.
3. L'orquestrador llegeix el mode efectiu de `collab_channel_config` si existeix; si no, fa fallback a `config`.
4. Canviar el mode durant una ronda activa no trenca la ronda — s'aplica a la propera iteració del bucle `run_round`.
5. Un editor pot canviar modes i aplicar perfils, però no editar perfils d'altres usuaris.
6. Els canals legacy migren automàticament al primer accés (lazy migration).

---

## W14 — Identitat visual

### Problema

Avui la barra d'agents (`CollabAgentsBar.svelte`) mostra cada agent com una pastilla amb el seu nom i estat. No hi ha diferenciació visual entre agents més enllà del nom. Falta:

1. **Color per agent:** cada agent té un color distintiu a la barra i als missatges.
2. **Avatar/emoji per agent:** un avatar visual o emoji que el representi.
3. **Rol visible:** el rol de l'agent (definit als overrides W12) es mostra a la UI.
4. **Contrast accessible:** els colors compleixen WCAG AA (ratio ≥ 4.5:1 per text normal).
5. **Prova de no pèrdua:** si un agent perd color/avatar, la barra encara mostra el nom i l'estat correctament.

### Arquitectura

#### 14.1 Font de dades

Els overrides W12 ja defineixen `color`, `avatar` i `role` per agent. El `resolve_agent()` de `profiles.py` els fusiona. Falta:

1. **Nou endpoint REST** que retorni la identitat efectiva de cada agent:
   ```
   GET /collab/{channel_id}/agents/identity
   → [{ "agent_id": "qwen...", "name": "Qwen", "color": "#e8854a", "avatar": "🧪", "role": "Tester" }]
   ```

2. **Frontend** (`collab/index.ts`) llegeix aquest endpoint i l'exposa a la barra i als missatges.

#### 14.2 Paleta de colors

Paleta de colors accessibles predefinida (fallback si l'override no té color):

| Color | Hex | Ús típic |
|---|---|---|
| Blau | `#3b82f6` | Default |
| Verd | `#10b981` | Tester/verificador |
| Taronja | `#f59e0b` | Implementador |
| Lila | `#8b5cf6` | Dissenyador |
| Rosa | `#ec4899` | Revisor |
| Cyan | `#06b6d4` | Documentació |
| Indigo | `#6366f1` | Coordinator |
| Vermell | `#ef4444` | Crític/auditor |

Els colors es validen contra WCAG AA: el backend comprova el contrast del `color` contra el fons del component (dark mode `#1e1e2e` i light mode `#f8f8f8`). Si no passa, es fa fallback al color per defecte.

**Implementació del contrast:** funció pura `has_good_contrast(hex_color, bg_hex) -> bool` que calcula el ratio de luminància segons WCAG 2.1. Es pot posar a `files.py` o en un mòdul `colors.py` nou.

#### 14.3 Avatar per agent

L'override W12 té `avatar` (string). Pot ser:

- Un emoji: `"🧪"`, `"🎨"`, `"⚙️"`
- Una URL d'imatge: `"https://..."` o ruta relativa: `"/avatars/qwen.png"`
- Un identificador d'icona (Lucide, FontAwesome): `"lucide:flask-conical"`

El frontend selecciona la representació segons el format:
- Comença per `http` o `/` → `<img>`
- Comença per `lucide:` → icona Lucide
- Altrament → emoji directament

#### 14.4 Frontend

A `CollabAgentsBar.svelte`:

```svelte
{#each agents as agent}
  <div class="agent-pill" style="--agent-color: {agent.color}">
    <span class="agent-avatar">{agent.avatar}</span>
    <span class="agent-name">{agent.name}</span>
    {#if agent.role}
      <span class="agent-role">{agent.role}</span>
    {/if}
    <span class="agent-state {agent.state}">{stateIcon(agent.state)}</span>
  </div>
{/each}
```

La pastilla usa la variable CSS `--agent-color` per:
- `border-left: 3px solid var(--agent-color)` — banda de color a l'esquerra
- `background: color-mix(in srgb, var(--agent-color) 10%, transparent)` — tint sutíl

Als missatges del canal, l'autor mostra l'avatar i el color com a vora del seu avatar.

#### 14.5 Prova de no pèrdua d'informació

**Test de regressió:** si `resolve_agent()` no troba cap override per un agent, retorna:
```python
{"color": None, "avatar": None, "role": None}
```

El frontend ha de gestionar aquests `None`:
- `color = null` → usa paleta per defecte (basada en hash del nom → color estable)
- `avatar = null` → mostra la primera lletra del nom com a avatar
- `role = null` → no mostra cap rol

Així la barra sempre és funcional, encara que els overrides no estiguin configurats.

### Criteris d'acceptació

1. Cada agent té un color, avatar i rol visibles a la barra d'agents i als missatges.
2. Els colors compleixen WCAG AA (ratio ≥ 4.5:1).
3. Si un agent no té override, la barra mostra un color/avatar/rol per defecte (no es perd informació).
4. El color i avatar es poden configurar per canal (via overrides W12) o per usuari (via perfil).
5. La identitat d'un agent es pot canviar en calent i es reflecteix a la barra sense refresh.
6. L'endpoint `/agents/identity` retorna la identitat efectiva de cada agent en una sola crida.

---

## Interacció amb altres W

| W | Interacció |
|---|---|
| **W11/W12** | Els perfils contenen els modes i els overrides (color/avatar/rol). W13/W14 els consumeixen. |
| **W1** (visibilitat) | La barra d'agents usa els colors/avatars de W14. |
| **W6** (UX) | Els errors de W6 usen el color de l'agent per a la identificació visual. |
| **W7** (i18n) | Els rols i noms d'estat han d'estar internacionalitzats. |

---

## Full de ruta d'implementació

### W13 (modes)

1. **Migració:** afegir `mode` + `conversation_mode` + `guardrails` a `collab_profile`. Migració Alembic.
2. **`profiles.py`:** `apply_profile()` copia els camps de mode. `resolve_agent()` exposa el mode efectiu.
3. **Orquestrador (Codex):** `run_round` llegeix `collab_channel_config` per al mode efectiu.
4. **Presets (seed data):** migració que crea els 4 presets com a perfils amb `is_template=True`.
5. **Router:** nou endpoint per llistar presets.
6. **Frontend (Claude Fable):** selector de presets al panell.
7. **Tests:** aplicar perfil canvia mode, mode canvia en calent, presets disponibles.

### W14 (identitat visual)

1. **`profiles.py`:** `resolve_agent()` ja retorna `color`/`avatar`/`rol`.
2. **Router:** nou endpoint `GET /{channel_id}/agents/identity`.
3. **Frontend (Claude Fable):** `CollabAgentsBar` usa color/avatar/rol. `collab/index.ts` fa crida a `/agents/identity`.
4. **Contrast:** funció `has_good_contrast()` a un mòdul utilitari.
5. **Fallback:** si no hi ha override, generar color per hash de nom + avatar per inicial.
6. **Tests:** endpoint retorna identitat, contrast valida, fallback funciona.

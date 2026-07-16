"""Comandes /collab escrites dins el canal (interfície provisional fins que hi
hagi UI pròpia — Fase 3 del pla)."""

import logging

from open_webui.collab.config import (
    GUARDRAIL_DEFAULTS,
    VALID_MODES,
    admin_only,
    get_collab_config,
    push_recent_dir,
    save_collab_config,
    validate_project_dir,
)
from open_webui.collab.orchestrator import (
    _get_models,
    is_round_active,
    post_notice,
    request_stop,
    run_round,
)
from open_webui.collab.tasks import get_phase, set_phase
from open_webui.utils.channels import extract_mentions

log = logging.getLogger(__name__)

HELP_TEXT = """**Comandes de l'espai col·laboratiu** (`/collab ...`)

- `/collab status` — configuració i estat actuals
- `/collab agents @Agent1 @Agent2 ...` — fixa els participants d'AQUESTA taula (menciona'ls amb @)
- `/collab agents add @Agent` / `remove @Agent` — afegeix/treu participants en calent
- `/collab dir <ruta>` — carpeta-projecte compartida (els agents hi treballen com al terminal); `dir none` per treure-la
- `/collab on` / `off` — activa/desactiva el mode col·laboratiu al canal
- `/collab mode handraise|roundrobin` — com es decideixen els torns
- `/collab guardrails` — mostra els guardarails; `guardrails clau=valor ...` per canviar-los en calent (0/off = desactivat)
- `/collab phase plan|exec` — força la fase de l'equip (📋 planificació / 🔨 execució)
- `/collab start` — llança una ronda ara mateix
- `/collab stop` — atura la ronda en curs

Amb el mode actiu, qualsevol missatge teu (que no sigui `/collab`) obre una ronda: cada agent decideix si vol intervenir i parlen per torns fins que ningú té res a afegir."""


def _parse_guardrail_value(key: str, raw: str):
    lowered = raw.strip().lower()
    default = GUARDRAIL_DEFAULTS.get(key)
    if isinstance(default, bool):
        if lowered in ("on", "true", "yes", "si", "sí", "1"):
            return True
        if lowered in ("off", "false", "no", "0", "none"):
            return False
        raise ValueError(f"`{key}` espera on/off")
    if lowered in ("off", "none", "no"):
        return 0
    return int(lowered)


async def _resolve_agent_ids(request, user, raw_content: str, tokens: list[str]) -> tuple[list[str], list[str]]:
    """Resol agents a partir de mencions <@M:id|label> i/o ids literals.
    Retorna (trobats, no_trobats)."""
    models = await _get_models(request, user)
    found: list[str] = []
    missing: list[str] = []

    for mention in extract_mentions(raw_content):
        if mention["id_type"] == "M" and mention["id"] not in found:
            if mention["id"] in models:
                found.append(mention["id"])
            else:
                missing.append(mention["id"])

    for token in tokens:
        candidate = token.strip().strip(",")
        if not candidate or candidate.startswith("<@") or candidate.startswith("@"):
            continue
        if candidate in found:
            continue
        if candidate in models:
            found.append(candidate)
        else:
            missing.append(candidate)

    return found, missing


async def handle_command(request, channel, message, user):
    content = (message.content or "").strip()
    parts = content.split()
    subcommand = parts[1].lower() if len(parts) > 1 else "help"
    args = parts[2:]

    config = get_collab_config(channel)

    async def reply(text: str):
        await post_notice(request, channel, user, text)

    try:
        # Amb COLLAB_ADMIN_ONLY=true, els no-admins només poden mirar (help/status).
        if admin_only() and user.role != "admin" and subcommand not in ("help", "?", "status"):
            await reply("⚠️ Només un admin pot gestionar aquest espai (COLLAB_ADMIN_ONLY actiu).")
            return

        if subcommand in ("help", "?"):
            await reply(HELP_TEXT)

        elif subcommand == "status":
            active = "▶️ Hi ha una ronda en curs." if is_round_active(channel.id) else "Cap ronda en curs."
            phase = await get_phase(channel.id)
            phase_label = "📋 planificació" if phase == "planning" else "🔨 execució"
            await reply(config.summary() + f"\n\n**Fase:** {phase_label}\n{active}")

        elif subcommand == "phase":
            if not args or args[0].lower() not in ("plan", "exec", "planning", "execution"):
                await reply("Ús: `/collab phase plan` (📋 planificació) o `/collab phase exec` (🔨 execució).")
                return
            new_phase = "planning" if args[0].lower().startswith("plan") else "execution"
            await set_phase(channel.id, new_phase)
            await reply(
                "📋 Fase canviada a **planificació**." if new_phase == "planning" else "🔨 Fase canviada a **execució**."
            )

        elif subcommand == "on":
            if not config.agents:
                await reply("⚠️ Primer defineix els participants: `/collab agents @Agent1 @Agent2`.")
                return
            config.enabled = True
            await save_collab_config(channel.id, config)
            await reply(
                "✅ Mode col·laboratiu **actiu**. Escriu l'objectiu i els agents començaran a treballar.\n\n"
                + config.summary()
            )

        elif subcommand == "off":
            config.enabled = False
            await save_collab_config(channel.id, config)
            request_stop(channel.id)
            await reply("⏸️ Mode col·laboratiu **desactivat** (el canal torna a funcionar amb mencions normals).")

        elif subcommand == "agents":
            action = args[0].lower() if args and args[0].lower() in ("add", "remove") else None
            tokens = args[1:] if action else args
            found, missing = await _resolve_agent_ids(request, user, content, tokens)

            if missing:
                await reply(
                    "⚠️ No conec aquests models: " + ", ".join(f"`{m}`" for m in missing)
                    + ". Menciona'ls amb `@` (autocompletat) o usa l'id exacte del model."
                )
            if not found:
                if not missing:
                    await reply("Indica els agents mencionant-los: `/collab agents @Claude @Codex`.")
                return

            if action == "add":
                config.agents = config.agents + [a for a in found if a not in config.agents]
            elif action == "remove":
                config.agents = [a for a in config.agents if a not in found]
            else:
                config.agents = found

            await save_collab_config(channel.id, config)
            await reply(
                "👥 Participants d'aquesta taula: "
                + (", ".join(f"`{a}`" for a in config.agents) if config.agents else "(cap)")
            )

        elif subcommand == "dir":
            if not args:
                await reply(
                    f"Carpeta actual: `{config.project_dir}`" if config.project_dir else "Aquest espai no té carpeta-projecte."
                )
                return
            raw_path = content.split(None, 2)[2].strip().strip('"')
            if raw_path.lower() in ("none", "off", "cap"):
                config.project_dir = None
                await save_collab_config(channel.id, config)
                await reply("🗂️ Carpeta-projecte eliminada de l'espai.")
                return
            ok, result = validate_project_dir(raw_path, is_admin=(user.role == "admin"))
            if not ok:
                await reply(f"⚠️ {result}")
                return
            config.project_dir = result
            await save_collab_config(channel.id, config)
            await push_recent_dir(result)
            await reply(
                f"🗂️ Carpeta-projecte fixada: `{result}`. Els agents CLI hi treballaran com a "
                "directori de treball a partir del proper torn."
            )

        elif subcommand == "mode":
            if not args or args[0].lower() not in VALID_MODES:
                await reply(f"Modes vàlids: {', '.join(f'`{m}`' for m in VALID_MODES)}.")
                return
            config.mode = args[0].lower()
            await save_collab_config(channel.id, config)
            await reply(f"⚙️ Mode de torns: `{config.mode}`.")

        elif subcommand == "guardrails":
            if not args:
                await reply(config.summary())
                return
            changes, errors = [], []
            for arg in args:
                if "=" not in arg:
                    errors.append(f"`{arg}` (format esperat: clau=valor)")
                    continue
                key, _, raw_value = arg.partition("=")
                key = key.strip()
                if key not in GUARDRAIL_DEFAULTS:
                    errors.append(f"`{key}` (desconegut; vàlids: {', '.join(GUARDRAIL_DEFAULTS)})")
                    continue
                try:
                    config.guardrails[key] = _parse_guardrail_value(key, raw_value)
                    changes.append(f"`{key}` = `{config.guardrails[key]}`")
                except ValueError as e:
                    errors.append(str(e))
            if changes:
                await save_collab_config(channel.id, config)
            text = ""
            if changes:
                text += "🛡️ Guardarails actualitzats (efecte immediat, també amb ronda en curs): " + ", ".join(changes)
            if errors:
                text += ("\n" if text else "") + "⚠️ Ignorats: " + ", ".join(errors)
            await reply(text or "Res a canviar.")

        elif subcommand == "start":
            if not (config.enabled and config.agents):
                await reply("⚠️ Activa primer el mode: `/collab agents @...` i `/collab on`.")
                return
            if is_round_active(channel.id):
                await reply("▶️ Ja hi ha una ronda en curs.")
                return
            await reply("▶️ Ronda iniciada.")
            await run_round(request, channel, user)

        elif subcommand == "stop":
            if request_stop(channel.id):
                await reply("⏹️ Aturant la ronda (acabarà el torn en curs i pararà).")
            else:
                await reply("No hi ha cap ronda en curs.")

        else:
            await reply(f"Comanda desconeguda: `{subcommand}`.\n\n{HELP_TEXT}")
    except Exception:
        log.exception("La comanda /collab ha fallat al canal %s", channel.id)
        await reply("💥 La comanda ha fallat; mira els logs del backend.")

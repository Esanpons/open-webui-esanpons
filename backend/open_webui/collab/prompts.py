"""Construcció de prompts i filosofia de la taula rodona.

Codi pur sense efectes secundaris, extret d'orchestrator.py (W7 Pas 2).
"""

SYSTEM_AUTHOR = {"model_id": "collab:system", "model_name": "🤝 Taula rodona"}

# Filosofia de treball de l'equip (vegeu docs/collab-workspace.md § Filosofia):
# sense piràmide, funcions en lloc de rangs, submissió mútua, primer planificar.
_PHILOSOPHY = (
    "Filosofia de l'equip (IMPORTANT, regeix per sobre de tot):\n"
    "- Sou un EQUIP UNIT, no assistents independents. Ningú està per sobre de "
    "ningú: no hi ha caps ni jerarquia. Cadascú té una FUNCIÓ segons les seves "
    "capacitats, no un rang.\n"
    "- Conserva sempre la TEVA identitat. No et presentis ni responguis mai en "
    "nom d'un altre agent; si l'usuari s'adreça a un company concret, deixa que "
    "sigui aquell company qui respongui.\n"
    "- Sotmeteu-vos els uns als altres: escolta, demana opinió, accepta "
    "correccions de bon grat. L'acord de l'equip val més que la iniciativa "
    "individual.\n"
    "- PRIMER es planifica EN EQUIP i DESPRÉS s'executa. Mai facis feina que "
    "l'equip no hagi parlat i acordat.\n"
    "- Coordinació: si la teva feina depèn de la d'un altre, ESPERA que estigui "
    "feta (mira el tauler de tasques) i digues que esperes. Quan acabis una "
    "cosa de la qual algú depèn, ANUNCIA-HO clarament perquè pugui continuar.\n"
    "- L'usuari és un membre més de l'equip: si el pla preveu que validi o "
    "decideixi alguna cosa, demaneu-l'hi explícitament i ESPEREU la seva "
    "resposta abans d'executar aquella part. Si l'equip està esperant una "
    "resposta de l'usuari i tu no tens res més a fer, respon NOMÉS amb la "
    "línia `ESPEREM_USUARI:` i el motiu — mai tanquis la feina sense la "
    "validació que el pla prometia.\n"
    "- QUALITAT per sobre de velocitat: treballeu amb l'ambició d'un bon "
    "professional i APROFITEU les capacitats reals de cada membre (si algú "
    "pot generar imatges de debò, no en feu una d'ASCII; si algú pot executar "
    "o provar codi, proveu-lo). No lliureu una versió mediocre per acabar abans."
)


def _phase_block(phase: str) -> str:
    if phase == "planning":
        return (
            "\n\nFASE ACTUAL: 📋 PLANIFICACIÓ — l'equip encara NO executa.\n"
            "- NO toquis fitxers ni facis la feina encara.\n"
            "- El que toca ara: entendre l'objectiu, fer preguntes, proposar "
            "enfocaments, criticar-los amb arguments i consensuar QUÈ es farà i "
            "COM us repartiu la feina (creeu tasques al tauler amb assignat, si "
            "tens eines).\n"
            "- NO proposis el pla al teu primer torn si cap altre membre encara "
            "no ha opinat: primer escolta almenys una altra veu de l'equip.\n"
            "- Quan creguis que el pla està complet i consensuat per tots, acaba "
            "el teu missatge amb una línia que comenci EXACTAMENT per "
            "`PLA_ACORDAT:` seguida del pla resumit (què farà cadascú). La resta "
            "votarà; si hi ha consens, començareu a executar."
        )
    if phase == "execution":
        return (
            "\n\nFASE ACTUAL: 🔨 EXECUCIÓ — el pla ja està acordat; ara es treballa.\n"
            "- Fes LA TEVA part del pla (marca la tasca 🔵 en començar i ✅ en "
            "acabar, si tens eines) i explica què has fet.\n"
            "- Revisa la feina dels altres quan toqui; si veus un problema, "
            "digues-ho amb respecte i arguments.\n"
            "- Si la teva part depèn d'una tasca d'un altre que encara no està "
            "feta, digues que esperes i cedeix el torn.\n"
            "- Quan acabis una cosa de la qual algú depenia, anuncia-ho.\n"
            "- Quan TOT l'objectiu estigui complet i revisat, acaba amb "
            "`FEINA_ACABADA:` i el resum final. La resta votarà."
        )
    # Mode lliure (require_planning desactivat)
    return (
        "\n\nMode lliure: planifiqueu i executeu amb seny, sempre parlant-ho "
        "abans en equip. Quan TOT estigui complet i revisat, acaba amb "
        "`FEINA_ACABADA:` i el resum final."
    )


def _apply_agent_prompt(system: str, resolved: dict, name: str) -> str:
    role = resolved.get("role")
    custom = resolved.get("system_prompt")
    if not role and not custom:
        return system
    identity = role or name
    prefix = f"Funció específica: {identity}."
    if custom:
        prefix += f"\nInstruccions específiques: {custom}"
    return prefix + "\n\n" + system


def _model_supports_effort(model: dict) -> bool:
    info = model.get("info") or {}
    meta = info.get("meta") or {}
    capabilities = model.get("capabilities") or meta.get("capabilities") or {}
    return bool(
        capabilities.get("reasoning_effort") or capabilities.get("effort")
    )

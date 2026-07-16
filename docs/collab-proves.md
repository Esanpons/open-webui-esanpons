# Guia de proves — Espai col·laboratiu (taula rodona d'IAs)

> Checklist per provar cada funcionalitat, una per una. Marca `[ ]` → `[x]` a mesura que ho vagis validant.
>
> **Abans de començar:** frontend a `http://localhost:5173`, backend al `8080` (⚠️ mai amb `--reload`). Agents recomanats per provar: **Claude Haiku (CLI)** i **Claude Sonnet (CLI)** — ràpids i barats. Carpeta de prova: `C:\Temp\Proves` (o la que vulguis, de prova).
>
> Guia de referència: [`collab-workspace.md`](collab-workspace.md) · Pla original: [`plans/espai-collaboratiu.md`](plans/espai-collaboratiu.md)

---

## 1. Taula rodona bàsica (les IAs col·laboren soles)

Cada missatge teu obre una **ronda**: mà alçada → torns per prioritat → es responen entre ells → fi per consens. Sense director.

- [ ] Sidebar → **Canals** → `+` → crea un canal (tipus "Canal") i entra-hi.
- [ ] Clica el botó **🤝** (a dalt a la dreta del xat) → s'obre el panell "Taula rodona".
- [ ] Afegeix 2 agents (Haiku + Sonnet) i prem **Activa l'espai**.
- [ ] Escriu al xat: *"Discutiu quina estructura hauria de tenir una web personal simple i poseu-vos d'acord."*
- [ ] ✔️ Els agents intervenen sols, per torns, i s'adrecen l'un a l'altre pel nom.

## 2. Agents per taula (diferents a cada canal)

- [ ] Amb la ronda en marxa, afegeix o treu un agent des del panell (✕ / desplegable + Afegeix) — té efecte al torn següent.
- [ ] Crea un segon canal amb agents diferents i comprova que cada taula manté la seva llista.

## 3. Carpeta-projecte (estil Claude/Codex al terminal)

- [ ] Panell → **📁 Tria una carpeta** → navega (C:\, D:\...) → **✔ Usa aquesta carpeta**.
- [ ] Alternativa: escriu la ruta directament al camp de text i prem **Usa**.
- [ ] Prova una ruta inexistent → ha de sortir un error clar.

## 4. Gestió de fitxers EXTERNA als models

Les eines de fitxers (llistar/llegir/escriure) les dona el sistema a qualsevol model — no depèn dels pipes de Claude/Codex. L'arbre s'injecta al context de tots.

- [ ] Amb carpeta posada, escriu: *"Creeu un fitxer NOTES.md amb 3 idees per a la web."*
- [ ] ✔️ El fitxer apareix de veritat a la carpeta del disc.
- [ ] (Futur: quan afegeixis Ollama o un model API, podrà fer el mateix sense tocar res.)

## 5. Avisos 🗂️ de canvis al projecte

- [ ] Després del torn del punt 4, al xat surt: *"🗂️ X ha tocat el projecte: 🆕 `NOTES.md`"*.
- [ ] Modifica tu un fitxer a mà i demana'ls una altra cosa → els canvis del seu torn es detecten igualment.

## 6. Arbre de fitxers en viu + visor

- [ ] Panell → **Fitxers del projecte**: l'arbre es refresca sol quan els agents escriuen.
- [ ] Clica un fitxer → s'obre el visor amb el contingut. Tanca amb ✕ o Escape.
- [ ] Botó **⟳** per refrescar manualment.

## 7. Guardarails (traduïts, amb tooltip, canviables en calent)

- [ ] Panell → **▼ Guardarails** → passa el ratolí per sobre de cada nom → surt l'explicació.
- [ ] Canvia **Màx. torns seguits** a `2`, desa, i llança una ronda → es pausa als 2 torns amb l'avís ⏸️.
- [ ] Torna-ho a deixar com vulguis (0 = sense límit). Els canvis valen fins i tot amb ronda en marxa.

## 8. Tauler de tasques compartit

- [ ] Panell → **Tasques de l'equip** → escriu "Maquetar la portada" → **Afegeix**.
- [ ] Canvia-li l'estat amb el desplegable (⬜ pendent / 🔵 en curs / ✅ feta) i esborra-la amb ✕.
- [ ] Demana als agents: *"Repartiu-vos la feina en tasques i mantingueu el tauler al dia."* → mira si creen/actualitzen tasques (els agents amb tools ho fan sols; els CLI veuen el tauler al context).

## 8b. Filosofia d'equip: primer planificar, després executar

L'equip comença en fase **📋 planificació** (xip al panell): parlen l'objectiu, es reparteixen la feina, i NO toquen fitxers. Quan un proposa `PLA_ACORDAT:` i la resta ho vota, passen a **🔨 execució**.

- [ ] Dona un objectiu i comprova que els primers torns són de DEBAT (propostes, preguntes, repartiment) sense tocar cap fitxer.
- [ ] ✔️ En algun moment: *"🗳️ X proposa donar el pla per acordat…"* → vot → *"📋 Pla acordat — comença l'execució 🔨"* i el xip del panell canvia.
- [ ] En execució, si un agent depèn d'un altre, diu que espera; quan l'altre acaba, ho anuncia i el primer continua.
- [ ] Per saltar-te la planificació: guardarail **Planificació primer** = desactivat, o `/collab phase exec`.

## 9. Consens explícit amb votació

- [ ] Dona un objectiu petit acabat amb: *"...i quan estigui fet i revisat, doneu la feina per acabada."*
- [ ] ✔️ Seqüència esperada: *"🗳️ X proposa donar la feina per acabada… "* → vot de la resta → *"✅ Consens: feina acabada (N a favor, M en contra)"* + resum final.
- [ ] Si no hi ha majoria: *"❌ Sense consens per tancar"* i la feina continua.

## 10. Resum incremental (memòria de l'espai)

- [ ] Quan acabi una ronda, mira el panell → secció **Resum de la feina** (l'escriu un agent "secretari").
- [ ] Llança una segona ronda i comprova que els agents recorden les decisions anteriors.
- [ ] Es pot desactivar amb el guardarail **Resum automàtic**.

## 11. Estadístiques i límits de cost

- [ ] En tancar-se qualsevol ronda, al xat surt: *"📊 Ronda: X torns d'agent · Y crides curtes · temps"*.
- [ ] Posa el guardarail **Límit de ronda (s)** a `120` i comprova que una ronda llarga es talla amb l'avís ⏱️.

## 12. Controls i badge

- [ ] Amb l'equip treballant: **⏹ Atura l'equip** (acaba el torn en curs i para).
- [ ] **▶ Posa l'equip a treballar** el reactiva sense escriure cap missatge nou.
- [ ] El xip d'estat del panell canvia: *inactiva* / *activa* / *equip treballant* (parpelleja).
- [ ] A la **sidebar**, el canal amb espai actiu porta el badge **🤝**.

## 12b. Treball continu (l'equip no espera botons)

- [ ] Dona un objectiu i NO toquis res: l'equip ha de planificar, votar el pla, executar i tancar **sense que premis cap botó**.
- [ ] Si es queden en silenci amb feina pendent, veuràs que un agent rep l'empenta del sistema i continua (no surt cap "ronda tancada" prematura).
- [ ] Només si de veritat no queda res a fer i ningú proposa tancar: *"😴 L'equip queda en repòs… escriu qualsevol missatge per reactivar-lo."*

## 13. Comandes `/collab` (alternativa al panell)

- [ ] Escriu `/collab help` al canal → surt l'ajuda completa.
- [ ] `/collab status` → configuració i estat actuals.
- [ ] `/collab stop` amb ronda en marxa → l'atura igual que el botó.

## 14. Permisos (opcional, per a més endavant)

- [ ] `COLLAB_ALLOWED_ROOTS=D:\Proyectos;C:\Temp` (variable d'entorn en arrencar el backend) → les carpetes fora de la llista es rebutgen.
- [ ] `COLLAB_ADMIN_ONLY=true` → només els admins poden configurar espais i gestionar rondes/tasques.

---

## 🎯 Prova final completa (toca gairebé tot de cop)

- [ ] Canal nou → Haiku + Sonnet → carpeta `C:\Temp\Proves` → **Activa l'espai** → escriu:

> *"Sou un equip. Creeu una mini web (index.html + style.css) amb una portada simple. Repartiu-vos la feina en tasques, reviseu-vos el codi l'un a l'altre, i quan estigui fet i revisat doneu la feina per acabada."*

Hauries de veure, en ordre: mà alçada → torns alternats → fitxers creats de veritat → avisos 🗂️ → (tasques al tauler) → proposta de tancament → 🗳️ votació → ✅ consens amb resum final → 📊 estadístiques → resum al panell.

---

## Si algo falla

1. Mira el missatge del canal: els errors surten com a avisos ⚠️ de la "🤝 Taula rodona".
2. Logs del backend (la finestra/tasca d'uvicorn): els errors dels pipes i de l'orquestrador hi queden amb traça completa.
3. Recorda: el backend **mai** amb `--reload` (els CLI no poden arrencar subprocessos i tots els torns fallen).
4. Casos coneguts i decisions: [`collab-workspace.md`](collab-workspace.md) § Limitacions.

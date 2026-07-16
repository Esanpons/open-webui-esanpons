# Generació d'imatges amb Codex

El pipe de Codex ([`integrations/codex_pipe.py`](../integrations/codex_pipe.py)) pot generar
imatges dins d'un xat normal d'Open WebUI i mostrar-les inline. Fa servir l'eina integrada
`image_gen` del Codex CLI (model `gpt-image-2`), **facturada contra la subscripció de
ChatGPT Plus/Pro** — sense clau d'API ni cost per imatge.

## Requisit: activar-ho al Codex CLI

A `~/.codex/config.toml` (a Windows: `%USERPROFILE%\.codex\config.toml`):

```toml
[features]
image_generation = true
```

**Sense aquesta línia, Codex diu que generarà la imatge i després no ho fa** — no dona cap
error, simplement inventa una excusa ("continuaré amb el generador d'imatges disponible")
i respon text. És el símptoma que identifica que falta el flag.

Comprovació ràpida, al terminal:

```bash
cd <una carpeta qualsevol>
echo "Crea una imatge d'un cargol i desa-la aqui com a cargol.png" | \
  codex exec --skip-git-repo-check -s workspace-write -
```

## Com funciona

| | Xat normal | Torn de taula rodona (amb carpeta-projecte) |
|---|---|---|
| Sandbox | `workspace-write` | el de la valve `COLLAB_SANDBOX` |
| Directori de treball | `DATA_DIR/cache/codex_images/<chat_id>` | la carpeta-projecte |
| Imatges | detectades i mostrades inline | sense canvis |

1. El pipe crea `DATA_DIR/cache/codex_images/<chat_id>` i hi fa córrer Codex.
2. Afegeix al prompt on ha de desar les imatges.
3. En acabar el torn, detecta els fitxers nous (`.png`, `.jpg`, `.webp`, `.gif`).
4. Els retorna com a markdown `![...](/cache/codex_images/<chat>/<fitxer>?v=<mtime>)`.

El backend serveix aquests fitxers per `/cache/...`
(`backend/open_webui/main.py`, `serve_cache_file`), que només mostra inline els MIME
`image/*`, `audio/*` i `video/*` i té protecció contra path traversal. Les imatges han de
viure sota `CACHE_DIR` — no serveix cap altra ruta.

El `?v=<mtime>` és un cache-buster: sense ell, reeditar una imatge amb el mateix nom
mostraria la versió antiga guardada al navegador.

**Per què `workspace-write` i no `read-only`**: abans els xats corrien read-only. Codex
generava la imatge a `$CODEX_HOME/generated_images/` però no la podia copiar enlloc
visible. Ara corre *dins* de la carpeta d'imatges, i `workspace-write` li limita
l'escriptura al `cwd` — pot desar la imatge i res més.

## Valve

`IMAGE_GENERATION` (per defecte activada) — Admin → Funcions → Codex → engranatge.
Desactiva-la per tornar al comportament antic (xats en read-only, sense imatges).

Només afecta els xats normals. Els torns de taula rodona amb carpeta-projecte mantenen
el seu sandbox (`COLLAB_SANDBOX`) i no es toquen.

## Sandbox de Windows sota AzureAD

Als logs hi veuràs errors com:

```
CreateProcessAsUserW failed: 5 (Acceso denegado)
```

És el problema conegut d'aquesta màquina: el sandbox de Windows denega arrencar
PowerShell. **No és fatal** — Codex genera la imatge igualment i fa servir `node_repl`
per moure el fitxer. Bug obert a upstream:
[openai/codex#19133](https://github.com/openai/codex/issues/19133) (afecta 0.120.0+).

## Desplegament

Editar `integrations/codex_pipe.py` **no és prou**: Open WebUI executa la còpia desada a
la taula `function` de cada base de dades. Cal enganxar el contingut a
**Admin → Funcions → Codex (ChatGPT Plus)** i reiniciar el backend, a cada instal·lació
(producció i dev tenen bases de dades separades).

## Neteja

Les imatges s'acumulen a `DATA_DIR/cache/codex_images/`. No es purguen soles; si ocupen
massa, esborra les carpetes dels xats antics — són només el cache, no hi ha res més.

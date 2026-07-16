"""Gestió de fitxers de la carpeta-projecte d'un espai col·laboratiu.

TOTA la gestió d'arxius és EXTERNA als models (requisit de disseny): aquí es
resolen rutes de forma segura, es construeix l'arbre, i es detecten canvis
entre torns. Qualsevol model (Ollama, APIs, pipes...) hi accedeix via les
eines de file_tools.py o via l'arbre injectat al context — mai depèn del
client concret (Claude Code, Codex...).
"""

import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Carpetes que mai es llisten ni es vigilen (soroll/pes).
IGNORED_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".svelte-kit",
    ".idea",
    ".vscode",
}

MAX_TREE_ENTRIES = 400  # límit dur de l'arbre (prompt i API)
MAX_SNAPSHOT_FILES = 20000  # límit del detector de canvis
MAX_FILE_BYTES = 512 * 1024  # límit de lectura d'un fitxer (eines i API)


def resolve_safe(project_dir: str, relative: str = ".") -> Optional[Path]:
    """Resol `relative` DINS de project_dir. Retorna None si s'escapa
    (.. , rutes absolutes fora, symlinks fora...)."""
    try:
        root = Path(project_dir).resolve()
        target = (root / (relative or ".")).resolve()
        if target == root or root in target.parents:
            return target
        return None
    except (OSError, ValueError):
        return None


def _iter_entries(root: Path):
    """Recorre el projecte (breadth-ish via os.walk) saltant IGNORED_DIRS."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".git"))
        rel_dir = os.path.relpath(dirpath, root)
        yield rel_dir, dirnames, sorted(filenames)


def build_tree(project_dir: str, max_entries: int = MAX_TREE_ENTRIES) -> dict:
    """Arbre pla i fitat: [{path, type, size}], ordenat per ruta. Retorna
    {'entries': [...], 'truncated': bool, 'total_listed': int}."""
    root = Path(project_dir)
    entries = []
    truncated = False

    if not root.is_dir():
        return {"entries": [], "truncated": False, "total_listed": 0}

    for rel_dir, dirnames, filenames in _iter_entries(root):
        prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        for d in dirnames:
            if len(entries) >= max_entries:
                truncated = True
                break
            entries.append({"path": f"{prefix}{d}", "type": "dir", "size": None})
        for f in filenames:
            if len(entries) >= max_entries:
                truncated = True
                break
            try:
                size = (root / rel_dir / f).stat().st_size
            except OSError:
                size = None
            entries.append({"path": f"{prefix}{f}", "type": "file", "size": size})
        if truncated:
            break

    return {"entries": entries, "truncated": truncated, "total_listed": len(entries)}


def tree_as_text(project_dir: str, max_entries: int = 150) -> str:
    """Versió text de l'arbre per injectar al context dels agents."""
    tree = build_tree(project_dir, max_entries=max_entries)
    if not tree["entries"]:
        return "(carpeta buida o inaccessible)"
    lines = []
    for e in tree["entries"]:
        if e["type"] == "dir":
            lines.append(f"{e['path']}/")
        else:
            size = f" ({e['size']} bytes)" if e["size"] is not None else ""
            lines.append(f"{e['path']}{size}")
    if tree["truncated"]:
        lines.append(f"... (llista tallada a {max_entries} entrades)")
    return "\n".join(lines)


def read_text_file(project_dir: str, relative: str, max_bytes: int = MAX_FILE_BYTES) -> tuple[bool, str]:
    """Llegeix un fitxer de text del projecte. Retorna (ok, contingut/motiu)."""
    target = resolve_safe(project_dir, relative)
    if target is None:
        return False, f"Ruta fora del projecte: {relative}"
    if not target.is_file():
        return False, f"No és un fitxer: {relative}"
    try:
        size = target.stat().st_size
        if size > max_bytes:
            return False, f"Fitxer massa gran ({size} bytes; màxim {max_bytes})."
        return True, target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return False, f"Error llegint {relative}: {e}"


def write_text_file(project_dir: str, relative: str, content: str) -> tuple[bool, str]:
    """Escriu (crea o sobreescriu) un fitxer de text dins del projecte."""
    target = resolve_safe(project_dir, relative)
    if target is None:
        return False, f"Ruta fora del projecte: {relative}"
    if target.is_dir():
        return False, f"És una carpeta: {relative}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return True, f"Escrit {relative} ({len(content)} caràcters)."
    except OSError as e:
        return False, f"Error escrivint {relative}: {e}"


def snapshot(project_dir: str) -> dict[str, tuple[float, int]]:
    """Foto {ruta_relativa: (mtime, size)} per detectar canvis entre torns."""
    root = Path(project_dir)
    result: dict[str, tuple[float, int]] = {}
    if not root.is_dir():
        return result
    count = 0
    for rel_dir, _dirnames, filenames in _iter_entries(root):
        prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
        for f in filenames:
            if count >= MAX_SNAPSHOT_FILES:
                return result
            try:
                st = (root / rel_dir / f).stat()
                result[f"{prefix}{f}"] = (st.st_mtime, st.st_size)
                count += 1
            except OSError:
                continue
    return result


def diff_snapshots(before: dict, after: dict) -> dict:
    """Canvis entre dues fotos: {'added': [...], 'modified': [...], 'deleted': [...]}"""
    added = sorted(p for p in after if p not in before)
    deleted = sorted(p for p in before if p not in after)
    modified = sorted(p for p in after if p in before and after[p] != before[p])
    return {"added": added, "modified": modified, "deleted": deleted}


def format_changes(author: str, changes: dict, limit: int = 15) -> Optional[str]:
    """Missatge 🗂️ per al canal, o None si no hi ha canvis."""
    parts = []
    for label, icon in (("added", "🆕"), ("modified", "✏️"), ("deleted", "🗑️")):
        for path in changes.get(label, []):
            parts.append(f"{icon} `{path}`")
    if not parts:
        return None
    shown = parts[:limit]
    more = len(parts) - len(shown)
    text = f"🗂️ **{author}** ha tocat el projecte:\n" + "\n".join(f"- {p}" for p in shown)
    if more > 0:
        text += f"\n- ... i {more} canvis més"
    return text


def list_dirs(path: str) -> list[dict]:
    """Subcarpetes directes d'una ruta (per al selector de carpeta de la UI)."""
    result = []
    try:
        base = Path(path)
        for child in sorted(base.iterdir()):
            if child.is_dir() and child.name not in IGNORED_DIRS and not child.name.startswith("."):
                result.append({"name": child.name, "path": str(child)})
    except OSError as e:
        log.debug("list_dirs(%s): %s", path, e)
    return result


def unique_timestamp() -> int:
    return int(time.time())

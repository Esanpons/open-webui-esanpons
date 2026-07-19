"""Tests de seguretat — W8 (S4 path traversal + S6 escapament LIKE).

S4: ``resolve_safe()`` ha de bloquejar tots els vectors d'escapament del
directori arrel del projecte:
- ``..`` simples i en codificacions
- rutes absolutes que apunten fora
- symlinks que apunten fora del projecte
- rutes amb separadors mixtes
- rutes buides o nul·les

S6: ``escape_like()`` escapa els comodins ``%`` i ``_`` de LIKE SQL.
"""

import importlib.metadata
import os
import sys
from pathlib import Path
from unittest.mock import patch

_real_version = importlib.metadata.version
importlib.metadata.version = lambda name: "0.0.0" if name == "open-webui" else _real_version(name)

from open_webui.collab.files import resolve_safe


# ---------------------------------------------------------------------------
# S4: resolve_safe — path traversal
# ---------------------------------------------------------------------------


def test_resolve_safe_root(tmp_path):
    """La relativa '.' o buida retorna l'arrel."""
    assert resolve_safe(str(tmp_path)) == tmp_path.resolve()
    assert resolve_safe(str(tmp_path), ".") == tmp_path.resolve()


def test_resolve_safe_valid_subpath(tmp_path):
    """Una subcarpeta dins del projecte és vàlida."""
    sub = tmp_path / "src" / "app.py"
    sub.parent.mkdir(parents=True)
    sub.touch()
    result = resolve_safe(str(tmp_path), "src/app.py")
    assert result == sub.resolve()


def test_resolve_safe_dotdot_escape(tmp_path):
    """``..`` que intenta sortir del projecte ha de retornar None."""
    assert resolve_safe(str(tmp_path), "../../../etc/passwd") is None
    assert resolve_safe(str(tmp_path), "../sibling") is None
    assert resolve_safe(str(tmp_path), "src/../../..") is None


def test_resolve_safe_absolute_outside(tmp_path):
    """Una ruta absoluta que apunta fora ha de retornar None."""
    outside = tmp_path.parent / "outside_target"
    result = resolve_safe(str(tmp_path), str(outside))
    assert result is None


def test_resolve_safe_absolute_inside(tmp_path):
    """Una ruta absoluta DINS del projecte és vàlida (resolve_safe resol
    la ruta real i comprova parents)."""
    inside = tmp_path / "nested" / "file.txt"
    inside.parent.mkdir(parents=True)
    inside.touch()
    result = resolve_safe(str(tmp_path), str(inside))
    assert result == inside.resolve()


def test_resolve_safe_symlink_escape(tmp_path):
    """Un symlink que apunta fora del projecte ha de retornar None."""
    outside = tmp_path.parent / "secret_outside"
    outside.mkdir(exist_ok=True)
    (outside / "stolen.txt").write_text("secret", encoding="utf-8")

    link = tmp_path / "escape_link"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        # Symlinks poden no estar disponibles (Windows sense permisos, CI
        # amb filesystem restringit). Saltar aquest test.
        return

    result = resolve_safe(str(tmp_path), "escape_link/stolen.txt")
    assert result is None


def test_resolve_safe_symlink_inside(tmp_path):
    """Un symlink que apunta DINS del projecte és vàlid."""
    real = tmp_path / "real_dir"
    real.mkdir()
    (real / "data.txt").write_text("ok", encoding="utf-8")

    link = tmp_path / "link_to_real"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        return

    result = resolve_safe(str(tmp_path), "link_to_real/data.txt")
    assert result == (real / "data.txt").resolve()


def test_resolve_safe_mixed_separators(tmp_path):
    """Separadors mixtes (/ i \\) no trenquen la resolució."""
    sub = tmp_path / "src" / "app"
    sub.mkdir(parents=True)
    (sub / "main.py").touch()
    # Amb / (POSIX)
    result = resolve_safe(str(tmp_path), "src/app/main.py")
    assert result == (sub / "main.py").resolve()
    # Amb \\ (Windows) — només rellevant a Windows
    if os.name == "nt":
        result = resolve_safe(str(tmp_path), "src\\app\\main.py")
        assert result == (sub / "main.py").resolve()


def test_resolve_safe_null_byte(tmp_path):
    """Null bytes a la ruta han de ser rebutjats (Path.resolve pot generar
    ValueError o retornar la ruta literal — tot i que sigui insegura)."""
    result = resolve_safe(str(tmp_path), "file\x00.txt")
    # Ha de retornar None o una ruta segura (dins del projecte)
    if result is not None:
        root = tmp_path.resolve()
        assert root in result.parents or result == root


def test_resolve_safe_none_relative(tmp_path):
    """relative=None s'interpreta com a '.' (arrel)."""
    result = resolve_safe(str(tmp_path), None)
    assert result == tmp_path.resolve()


def test_resolve_safe_nonexistent_project(tmp_path):
    """Un project_dir que no existeix: resolve_safe encara funciona
    (resol la ruta literal i comprova parents)."""
    ghost = tmp_path / "ghost_project"
    result = resolve_safe(str(ghost), "some_file.txt")
    assert result is None or ghost.resolve() in result.parents


def test_resolve_safe_dotdot_in_middle(tmp_path):
    """``.``'s al mig que es col·lapsen correctament."""
    sub = tmp_path / "real"
    sub.mkdir()
    (sub / "file.txt").touch()
    result = resolve_safe(str(tmp_path), "real/./file.txt")
    assert result == (sub / "file.txt").resolve()
    # real/../real/file.txt també és vàlid (no surt fora)
    result = resolve_safe(str(tmp_path), "real/../real/file.txt")
    assert result == (sub / "file.txt").resolve()


def test_resolve_safe_case_sensitivity(tmp_path):
    """A Windows (case-insensitive), una ruta amb casing diferent
    encara s'ha de validar correctament."""
    sub = tmp_path / "MyDir"
    sub.mkdir()
    (sub / "File.txt").touch()
    result = resolve_safe(str(tmp_path), "mydir/file.txt")
    if os.name == "nt":
        # A Windows la resolució és case-insensitive
        assert result is not None
    else:
        # A POSIX, si la carpeta existeix amb casing diferent, Path.resolve
        # no la troba (retorna la ruta literal, que NO està dins del projecte)
        assert result is None or result == (tmp_path / "mydir" / "file.txt").resolve()


# ---------------------------------------------------------------------------
# S6: escape_like — escapament de comodins LIKE
# ---------------------------------------------------------------------------


def test_escape_like_basic():
    """El % i _ s'escapen amb backslash."""
    from open_webui.collab.files import escape_like

    assert escape_like("normal") == "normal"
    assert escape_like("100%") == "100\\%"
    assert escape_like("file_name") == "file\\_name"
    assert escape_like("a_b%c") == "a\\_b\\%c"


def test_escape_like_empty():
    """String buit retorna buit."""
    from open_webui.collab.files import escape_like

    assert escape_like("") == ""
    assert escape_like(None) == ""


def test_escape_like_backslash():
    """Un backslash literal es duplica (escapar l'escapador)."""
    from open_webui.collab.files import escape_like

    assert escape_like("path\\to") == "path\\\\to"
    assert escape_like("path\\to_%") == "path\\\\to\\_\\%"


def test_escape_like_multiple():
    """Múltiples ocurrències s'escapen totes."""
    from open_webui.collab.files import escape_like

    assert escape_like("%%__") == "\\%\\%\\_\\_"


# ---------------------------------------------------------------------------
# MR-24: null byte rebutjat explícitament + bloqueig d'escriptura a .git/
# ---------------------------------------------------------------------------


def test_resolve_safe_null_byte_now_rejected(tmp_path):
    """El null byte ara es rebutja SEMPRE (retorna None), no depèn del runtime."""
    assert resolve_safe(str(tmp_path), "file\x00.txt") is None
    assert resolve_safe(str(tmp_path), "a/b\x00/c") is None


def test_write_blocked_in_git_dir(tmp_path):
    """No es pot escriure dins de .git/ (vector RCE via hooks)."""
    from open_webui.collab.files import write_text_file

    ok, reason = write_text_file(str(tmp_path), ".git/hooks/pre-commit", "#!/bin/sh\nrm -rf /")
    assert ok is False
    assert ".git" in reason


def test_write_blocked_in_node_modules(tmp_path):
    from open_webui.collab.files import write_text_file

    ok, _ = write_text_file(str(tmp_path), "node_modules/evil/index.js", "x")
    assert ok is False


def test_write_allowed_in_normal_path(tmp_path):
    from open_webui.collab.files import write_text_file

    ok, _ = write_text_file(str(tmp_path), "src/app.py", "print('hi')")
    assert ok is True
    assert (tmp_path / "src" / "app.py").read_text() == "print('hi')"


# ---------------------------------------------------------------------------
# MR-12: validate_project_dir amb whitelist i mode local
# ---------------------------------------------------------------------------


def test_validate_project_dir_within_whitelist(tmp_path, monkeypatch):
    from open_webui.collab import config as collab_config

    root = tmp_path / "allowed"
    inside = root / "proj"
    inside.mkdir(parents=True)
    monkeypatch.setenv("COLLAB_ALLOWED_ROOTS", str(root))
    ok, result = collab_config.validate_project_dir(str(inside), is_admin=False)
    assert ok is True
    assert os.path.abspath(str(inside)) == result


def test_validate_project_dir_outside_whitelist(tmp_path, monkeypatch):
    from open_webui.collab import config as collab_config

    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setenv("COLLAB_ALLOWED_ROOTS", str(root))
    ok, _ = collab_config.validate_project_dir(str(outside), is_admin=True)
    assert ok is False


def test_validate_project_dir_no_whitelist_non_local_rejected(tmp_path, monkeypatch):
    """Sense whitelist i sense mode local: es rebutja fins i tot per admin."""
    from open_webui.collab import config as collab_config

    monkeypatch.delenv("COLLAB_ALLOWED_ROOTS", raising=False)
    monkeypatch.setenv("COLLAB_LOCAL_MODE", "false")
    proj = tmp_path / "proj"
    proj.mkdir()
    ok, _ = collab_config.validate_project_dir(str(proj), is_admin=True)
    assert ok is False


def test_validate_project_dir_local_admin_ok(tmp_path, monkeypatch):
    from open_webui.collab import config as collab_config

    monkeypatch.delenv("COLLAB_ALLOWED_ROOTS", raising=False)
    monkeypatch.setenv("COLLAB_LOCAL_MODE", "true")
    proj = tmp_path / "proj"
    proj.mkdir()
    ok, _ = collab_config.validate_project_dir(str(proj), is_admin=True)
    assert ok is True


# ---------------------------------------------------------------------------
# MR-07/08: sanitize_project_dir i sanitize_overrides
# ---------------------------------------------------------------------------


def test_sanitize_project_dir_strips_invalid(tmp_path, monkeypatch):
    from open_webui.collab.profiles import sanitize_project_dir

    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("COLLAB_ALLOWED_ROOTS", str(root))
    # Carpeta fora de la whitelist → s'elimina project_dir de la config.
    out = sanitize_project_dir({"project_dir": str(tmp_path / "evil"), "agents": ["a1"]}, is_admin=True)
    assert "project_dir" not in out
    assert out["agents"] == ["a1"]


def test_sanitize_overrides_drops_invalid():
    from open_webui.collab.profiles import sanitize_overrides

    overrides = [
        {"model_id": "a1", "priority": 3},          # vàlid
        {"model_id": "a2", "priority": 999},        # priority fora de rang → descartat
        {"display_name": "sense model"},            # sense model_id → descartat
        {"model_id": "a3", "effort": "banana"},     # effort no vàlid? (opcional) — depèn del model
    ]
    clean = sanitize_overrides(overrides)
    ids = {o["model_id"] for o in clean}
    assert "a1" in ids
    assert "a2" not in ids  # priority 999 invàlida
    # a3 amb effort lliure: AgentOverride no restringeix effort a un enum, així
    # que passa; el que importa és que a1 hi és i a2 no.
    assert all(o.get("model_id") for o in clean)


# ---------------------------------------------------------------------------
# MR-13: _require_channel_manager
# ---------------------------------------------------------------------------


def test_require_channel_manager_admin_ok():
    from types import SimpleNamespace
    from open_webui.collab.router import _require_channel_manager

    admin = SimpleNamespace(id="u1", role="admin")
    channel = SimpleNamespace(user_id="owner", id="c1")
    _require_channel_manager(channel, admin)  # no llança


def test_require_channel_manager_owner_ok():
    from types import SimpleNamespace
    from open_webui.collab.router import _require_channel_manager

    owner = SimpleNamespace(id="owner", role="user")
    channel = SimpleNamespace(user_id="owner", id="c1")
    _require_channel_manager(channel, owner)  # no llança


def test_require_channel_manager_stranger_forbidden():
    from types import SimpleNamespace
    from fastapi import HTTPException
    from open_webui.collab.router import _require_channel_manager

    stranger = SimpleNamespace(id="u2", role="user")
    channel = SimpleNamespace(user_id="owner", id="c1")
    try:
        _require_channel_manager(channel, stranger)
        assert False, "hauria d'haver llançat 403"
    except HTTPException as e:
        assert e.status_code == 403

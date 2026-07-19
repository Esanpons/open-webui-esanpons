import importlib.metadata
import os
import stat

_real_version = importlib.metadata.version
importlib.metadata.version = lambda name: "0.0.0" if name == "open-webui" else _real_version(name)

from open_webui.collab import files


def test_write_text_file_is_atomic_and_preserves_permissions(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "note.txt"
    target.parent.mkdir()
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)
    replacements = []
    real_replace = os.replace

    def capture_replace(source, destination):
        source_path = tmp_path.__class__(source)
        assert source_path.parent == target.parent
        assert source_path.read_text(encoding="utf-8") == "nou contingut"
        replacements.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(files.os, "replace", capture_replace)

    ok, detail = files.write_text_file(str(tmp_path), "nested/note.txt", "nou contingut")

    assert ok, detail
    assert target.read_text(encoding="utf-8") == "nou contingut"
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert len(replacements) == 1
    assert not list(target.parent.glob(".collab_write_*.tmp"))


def test_write_text_file_rejects_utf8_payload_over_byte_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "MAX_FILE_BYTES", 3)

    ok, detail = files.write_text_file(str(tmp_path), "large.txt", "éé")

    assert not ok
    assert "4 bytes" in detail
    assert not (tmp_path / "large.txt").exists()


def test_write_failure_keeps_original_and_removes_temporary(tmp_path, monkeypatch):
    target = tmp_path / "note.txt"
    target.write_text("original", encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(files.os, "replace", fail_replace)

    ok, detail = files.write_text_file(str(tmp_path), "note.txt", "replacement")

    assert not ok
    assert "simulated replace failure" in detail
    assert target.read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob(".collab_write_*.tmp"))


def test_cleanup_only_removes_old_collab_temporaries(tmp_path):
    old_temp = tmp_path / ".collab_write_old.tmp"
    fresh_temp = tmp_path / ".collab_write_fresh.tmp"
    unrelated = tmp_path / "other.tmp"
    old_temp.write_text("old", encoding="utf-8")
    fresh_temp.write_text("fresh", encoding="utf-8")
    unrelated.write_text("other", encoding="utf-8")
    os.utime(old_temp, (1, 1))

    removed = files.cleanup_temp_files(str(tmp_path), min_age_seconds=60)

    assert removed == 1
    assert not old_temp.exists()
    assert fresh_temp.exists()
    assert unrelated.exists()

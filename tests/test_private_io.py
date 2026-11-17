import os
import stat
from pathlib import Path

import pytest

from ai_memory.private_io import write_private_text


def test_private_writer_creates_private_file_before_writing_and_replaces_atomically(
    tmp_path, monkeypatch
):
    import tempfile

    tmp_path.chmod(0o755)
    path = tmp_path / "archive.md"
    path.write_text("old synthetic content", encoding="utf-8")
    path.chmod(0o644)
    text = "New synthetic text\r\nМонгол\n"
    real_mkstemp = tempfile.mkstemp
    real_replace = os.replace
    real_fsync = os.fsync
    stages = []

    def observe_creation(*args, **kwargs):
        descriptor, name = real_mkstemp(*args, **kwargs)
        metadata = os.fstat(descriptor)
        assert stat.S_IMODE(metadata.st_mode) == 0o600
        assert metadata.st_size == 0
        assert Path(name).parent == tmp_path
        assert Path(name) != path
        stages.append("created")
        return descriptor, name

    def observe_fsync(descriptor):
        assert stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o600
        assert path.read_text(encoding="utf-8") == "old synthetic content"
        stages.append("fsync")
        return real_fsync(descriptor)

    def observe_replace(source, destination):
        assert Path(source).read_bytes() == text.encode("utf-8")
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        assert path.read_text(encoding="utf-8") == "old synthetic content"
        assert Path(destination) == path
        assert stages == ["created", "fsync"]
        stages.append("replace")
        return real_replace(source, destination)

    monkeypatch.setattr(tempfile, "mkstemp", observe_creation)
    monkeypatch.setattr(os, "fsync", observe_fsync)
    monkeypatch.setattr(os, "replace", observe_replace)

    write_private_text(path, text)

    assert path.read_bytes() == text.encode("utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755
    assert stages == ["created", "fsync", "replace"]
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("stage", ["fsync", "replace"])
def test_private_writer_failure_preserves_destination_and_cleans_temporary_file(
    tmp_path, monkeypatch, stage
):
    path = tmp_path / "archive.md"
    path.write_text("old synthetic content", encoding="utf-8")

    def fail(*args, **kwargs):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(os, stage, fail)
    with pytest.raises(OSError, match="synthetic write failure"):
        write_private_text(path, "new synthetic content")

    assert path.read_text(encoding="utf-8") == "old synthetic content"
    assert list(tmp_path.iterdir()) == [path]


def test_private_writer_failure_before_encoding_cleans_temporary_file(tmp_path):
    path = tmp_path / "archive.md"

    with pytest.raises(UnicodeEncodeError):
        write_private_text(path, "synthetic unpaired surrogate \ud800")

    assert list(tmp_path.iterdir()) == []


def test_private_writer_requires_existing_parent_without_changing_it(tmp_path):
    parent = tmp_path / "missing-parent"

    with pytest.raises(FileNotFoundError):
        write_private_text(parent / "archive.md", "synthetic content")

    assert not parent.exists()

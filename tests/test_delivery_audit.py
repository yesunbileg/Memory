"""Public entry points and package boundaries involved in the delivery audit."""

import asyncio
import importlib.util
import json
import threading
from pathlib import Path

import pytest
from test_extension_package import package, package_source
from test_store import conversation

from ai_memory import cli
from ai_memory.demo import run_demo
from ai_memory.store import Store

ROOT = Path(__file__).resolve().parents[1]


def live_module():
    spec = importlib.util.spec_from_file_location("live_demo", ROOT / "scripts/live_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_status_typo_has_no_filesystem_side_effect(tmp_path, capsys):
    directory = tmp_path / "typo"
    assert cli.main(["--db", str(directory / "archive.sqlite"), "status"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "archive_not_found"
    assert not directory.exists()


def test_slow_poll_does_not_block_next_local_scan_and_persists_health(tmp_path, monkeypatch):
    db = tmp_path / "archive.sqlite"
    with Store(db) as store:
        store.set_setting("notebook_id", "demo")
    entered, released = threading.Event(), threading.Event()
    finished = threading.Event()
    observed = []

    async def slow_sync(store):
        entered.set()
        await asyncio.to_thread(released.wait, 1)
        finished.set()
        return {"status": {"sync_status": "up_to_date"}}

    def scan(*args, **kwargs):
        observed.append(entered.is_set() and not finished.is_set())
        return {"failed_files": 0}

    def tick(_seconds):
        assert entered.wait(1)
        if len(observed) == 2:
            released.set()
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "sync_archive", slow_sync)
    monkeypatch.setattr(cli, "collect_into", scan)
    monkeypatch.setattr(cli, "_sleep", tick)
    monkeypatch.setattr(cli.os, "getloadavg", lambda: (0, 0, 0))
    try:
        with pytest.raises(KeyboardInterrupt):
            cli._watch(cli.parser().parse_args(["--db", str(db), "watch", "--codex-path", "x"]))
    finally:
        released.set()
    with Store(db) as store:
        assert observed == [False, True]
        assert store.status()["watch"]["sources"]["codex"]["last_successful_scan"]


def test_live_demo_refuses_existing_mixed_approved_archive_before_provider(tmp_path, monkeypatch):
    output = tmp_path / "demo"
    run_demo(output)
    with Store(output / "conversations.sqlite") as store:
        store.ingest(conversation(), consent=True)
        store.set_setting("notebook_id", "explicit-test-id")
    module = live_module()
    calls = []

    async def should_not_sync(store):
        calls.append(True)
        return {"blocked": [], "status": {"sync_status": "pending"}}

    monkeypatch.setattr(module, "sync_archive", should_not_sync)
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(module.live_demo(output, notebook_id="explicit-test-id"))
    assert not calls


def test_live_demo_requires_explicit_notebook_id_before_preparation(tmp_path):
    with pytest.raises(ValueError, match="notebook"):
        asyncio.run(live_module().live_demo(tmp_path / "empty", notebook_id=""))
    assert not (tmp_path / "empty").exists()


def test_zip_only_reachable_assets_both_licenses_and_no_version_collision(tmp_path):
    import zipfile

    source = package_source(tmp_path)
    (source / "debug.js").write_text("unreferenced private script")
    out = tmp_path / "extension.zip"
    assert package(source, out).returncode == 0
    with zipfile.ZipFile(out) as archive:
        assert "debug.js" not in archive.namelist()
        assert archive.read("LICENSE.ai-memory") == (ROOT / "LICENSE").read_bytes()
        assert archive.read("LICENSE") == (ROOT / "vendor/hstry/LICENSE").read_bytes()
    original = out.read_bytes()
    assert package(source, out).returncode == 0
    with (source / "background.js").open("a") as stream:
        stream.write("\n// different packaged bytes\n")
    assert package(source, out).returncode != 0
    assert out.read_bytes() == original


def test_release_version_must_advance_if_bytes_change(tmp_path):
    import zipfile

    def artifact(name, version, payload):
        path = tmp_path / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"version": version}))
            archive.writestr("background.js", payload)
        return path

    spec = importlib.util.spec_from_file_location(
        "release_record", ROOT / "scripts/release_record.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    past = artifact("old.zip", "0.5.3", "old runtime")
    same = artifact("same.zip", "0.5.3", "new runtime")
    new = artifact("new.zip", "0.6.0", "new runtime")
    with pytest.raises(ValueError, match="version"):
        module.check_version(past, same)
    module.check_version(past, past)
    module.check_version(past, new)


def test_live_demo_revalidates_all_local_conversations_before_construction(tmp_path, monkeypatch):
    module = live_module()
    calls = []

    def polluted_fixture(path):
        data = run_demo(path)
        with Store(path / "conversations.sqlite") as store:
            store.ingest(conversation(), consent=True)
        return data

    monkeypatch.setattr(module, "run_demo", polluted_fixture)
    monkeypatch.setattr(module, "NotebookLMAdapter", lambda *_: calls.append("constructed"))
    with pytest.raises(ValueError, match="fixture"):
        asyncio.run(module.live_demo(tmp_path / "new", notebook_id="chosen-id"))
    assert calls == []


def test_nonempty_selected_notebook_gets_no_upload(tmp_path, monkeypatch):
    module = live_module()
    events = []

    class Nonempty:
        def __init__(self, notebook_id):
            assert notebook_id == "chosen-id"

        async def list_sources(self):
            events.append("checked")
            return [{"id": "unrelated"}]

        async def close(self):
            events.append("closed")

    monkeypatch.setattr(module, "NotebookLMAdapter", Nonempty)
    with pytest.raises(ValueError, match="empty notebook"):
        asyncio.run(module.live_demo(tmp_path / "new", notebook_id="chosen-id"))
    assert events == ["checked", "closed"]


@pytest.mark.parametrize("kind", ["empty", "foreign", "text"])
def test_status_does_not_initialize_or_modify_a_nonarchive(tmp_path, kind, capsys):
    import sqlite3

    path = tmp_path / "wrong.sqlite"
    path.touch()
    if kind == "foreign":
        with sqlite3.connect(path) as foreign:
            foreign.execute("CREATE TABLE inventory(item INTEGER)")
    if kind == "text":
        path.write_text("This is a personal note, not an archive.")
    path.chmod(0o644)
    original = path.read_bytes()
    assert cli.main(["--db", str(path), "status"]) != 0
    assert "error" in json.loads(capsys.readouterr().out)
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o644
    assert sorted(tmp_path.iterdir()) == [path]

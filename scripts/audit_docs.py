"""Trace a new synthetic Codex message through Docs and NotebookLM in an empty test notebook."""

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx

from ai_memory.archive_files import backup_archive, restore_archive
from ai_memory.cli import sync_archive
from ai_memory.diagnostics import failure_code
from ai_memory.gateway import MemoryApplication
from ai_memory.notebook import NotebookLMAdapter
from ai_memory.privacy import snapshot_sha256
from ai_memory.private_io import write_private_text
from ai_memory.publication import allowed_sources
from ai_memory.store import Store
from ai_memory.web_docs_background import BACKGROUND_SESSION, BackgroundDocsTransport
from ai_memory.web_docs_journal import INDEX_RECEIPT_PREFIX, DocumentJournal, split_document
from ai_memory.web_docs_protocol import editor_commands
from ai_memory.web_docs_publication import enable_web_docs
from ai_memory.web_text_delta import text_edits

MARKER = "UNCHANGEDFIELDLOGSENTINEL"
QUESTION = "Quote the complete sentence from the field log about the winter observatory's spare compass and cite the original message."
BLOCKERS = {
    "auth_required",
    "docs_web_auth_required",
    "docs_web_brave_required",
    "docs_web_unavailable",
    "network_error",
    "rate_limited",
    "not_ready",
    "timeout",
    "SourceQuota",
    "notebooklm_error",
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class Evidence:
    def __init__(self, folder):
        self.path = folder / "evidence.json"
        self.checks = {}
        self.network = defaultdict(Counter)
        self.phase = "setup"
        self.start = time.monotonic()

    def record(self, name, status, **detail):
        self.checks[name] = {"status": status, **detail}
        self.save()
        print(f"{name} {status}", flush=True)

    def verify(self, name, passed, **detail):
        self.record(name, "PASS" if passed else "FAIL", **detail)
        return passed

    def save(self):
        write_private_text(
            self.path,
            json.dumps(
                {
                    "elapsed_s": round(time.monotonic() - self.start, 2),
                    "checks": self.checks,
                    "notebook_http": self.network,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    @contextmanager
    def measure_notebook(self):
        # Count only HTTP size metadata in this dedicated audit process.
        send = httpx.AsyncClient.send
        raw = httpx.Response.aiter_raw
        pending = {}

        async def count_raw(response, chunk_size=None):
            tracked = pending.get(id(response))
            complete = False
            try:
                async for chunk in raw(response, chunk_size):
                    yield chunk
                complete = True
            finally:
                if tracked is not None and pending.pop(id(response), None) is not None:
                    _, stats = tracked
                    stats["known_response_body_bytes"] += response.num_bytes_downloaded
                    stats[
                        "streamed_responses_completed"
                        if complete
                        else "streamed_responses_incomplete"
                    ] += 1

        async def counted(client, request, **kwargs):
            stats = self.network[self.phase]
            stats["requests"] += 1
            length = request.headers.get("content-length")
            if length is not None and length.isdigit():
                stats["known_request_body_bytes"] += int(length)
            elif request.method not in {"GET", "HEAD"}:
                stats["unknown_request_sizes"] += 1
            try:
                response = await send(client, request, **kwargs)
            except Exception:
                stats["failed_requests"] += 1
                raise
            if response.is_closed:
                stats["known_response_body_bytes"] += response.num_bytes_downloaded
            else:
                stats["streamed_responses_opened"] += 1
                pending[id(response)] = (response, stats)
            if response.is_error:
                stats["http_errors"] += 1
            return response

        with (
            patch.object(httpx.AsyncClient, "send", counted),
            patch.object(httpx.Response, "aiter_raw", count_raw),
        ):
            try:
                yield
            finally:
                # A response never consumed (or not exhausted) is explicitly incomplete.
                for response, stats in pending.values():
                    stats["known_response_body_bytes"] += response.num_bytes_downloaded
                    stats["streamed_responses_unmeasured"] += 1
                pending.clear()


async def browser_audit(docs, action, **options):
    program = Path(__file__).with_name("audit_browser.js").read_text()
    with tempfile.TemporaryDirectory(prefix="memory-audit-meter-") as tmp:
        path = Path(tmp) / "meter.js"
        params = {"action": action, **options}
        write_private_text(path, f"const auditInput = {json.dumps(params)};\n" + program)
        stdout, status = await docs._run_script(path)
    prefix = "AI_MEMORY_DOCS_RESULT="
    if status or not stdout.startswith(prefix):
        raise RuntimeError("audit_meter_unavailable")
    result = json.loads(stdout[len(prefix) :])
    if result.get("status") != "ok":
        raise RuntimeError("audit_meter_unavailable")
    return result.get("metrics", {})


def capture(db, path, session_id, seed, sentence=None):
    created = "2026-01-10T01:00:00Z"
    records = [{"type": "session_meta", "payload": {"id": session_id, "timestamp": created}}]
    for number, body in enumerate([seed, *([sentence] if sentence else [])], 1):
        records.append(
            {
                "type": "response_item",
                "timestamp": f"2026-01-10T01:00:0{number}Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "id": f"field-note-{number}",
                    "content": [{"type": "input_text", "text": body}],
                },
            }
        )
    write_private_text(path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records))
    event = {"hook_event_name": "Stop", "session_id": session_id, "transcript_path": str(path)}
    subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_memory",
            "--db",
            str(db),
            "hook",
            "codex",
            "--account",
            "synthetic-field-log",
        ],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        check=True,
        timeout=45,
    )


def state_digest(store):
    db = store.connection
    messages = [
        list(row)
        for row in db.execute("SELECT id,sequence,text,content_hash FROM messages ORDER BY id")
    ]
    jobs = [
        list(row)
        for row in db.execute(
            "SELECT * FROM sync_jobs WHERE stage NOT IN ('done','superseded') ORDER BY id"
        )
    ]
    return {
        "quick_check": db.execute("PRAGMA quick_check").fetchone()[0],
        "messages": len(messages),
        "messages_sha256": digest(messages),
        "pending_jobs": len(jobs),
        "pending_sha256": digest(jobs),
    }


def check_restore(store, folder, report):
    before = state_digest(store)
    backup = folder / "backup.sqlite"
    restored = folder / "restored.sqlite"
    backup_archive(store, backup)
    restore_archive(restored, backup)
    with Store(restored, existing=True) as copy:
        after = state_digest(copy)
    report.verify("backup_restore", before == after and after["quick_check"] == "ok", **after)


async def publication_cycle(store, report, name, docs):
    begin = time.monotonic()
    result = await sync_archive(store, docs_client=docs)
    blockers = [item.get("reason", "unknown") for item in result["blocked"]]
    status = result["status"]["sync_status"]
    passed = not blockers and status == "up_to_date"
    report.record(
        name,
        "PASS"
        if passed
        else ("BLOCKED" if not blockers or all(code in BLOCKERS for code in blockers) else "FAIL"),
        stopped_at=status,
        reasons=blockers,
        seconds=round(time.monotonic() - begin, 2),
        confirmed_save_body_bytes=result.get("docs_web_write_payload_bytes"),
    )
    return passed


def source_pair(store):
    row = store.connection.execute(
        "SELECT segment_id,source_id,document_id FROM google_docs_parts WHERE part=0"
    ).fetchone()
    return dict(row) if row else {}


async def check_mirror_repair(store, docs, report, phase, pair, sentence):
    doc, source_id = pair["document_id"], pair["source_id"]
    journal = DocumentJournal(store, docs, doc)
    original = journal._load()["text"]
    damage = "Synthetic manual edit " + uuid4().hex
    changed = original.replace(sentence, damage, 1)
    if changed == original:
        report.record("mirror_repair", "BLOCKED", reason="no_verified_document")
        return
    old_state = state_digest(store)
    old_revision = allowed_sources(store)[source_id]["source_revision"]
    await phase("manual_edit")
    before = snapshot_sha256(original)
    if (await docs.call("prepare", id=doc, known=before)).get("status") != "ready":
        report.record("mirror_repair", "BLOCKED", reason="no_verified_document")
        return
    await docs.write(
        doc,
        uuid4().hex,
        before,
        snapshot_sha256(changed),
        editor_commands(text_edits(original, changed)),
        recovering=False,
    )
    await phase("repair")
    if not await publication_cycle(store, report, "repair_publication", docs):
        report.record("mirror_repair", "BLOCKED", reason="repair_not_verified")
        return
    restored = journal._load()["text"]
    await journal.verify_remote(restored)
    new_state = state_digest(store)
    revision = allowed_sources(store)[source_id]["source_revision"]
    report.verify(
        "mirror_repair",
        restored.count(sentence) == 1
        and damage not in restored
        and pair == source_pair(store)
        and old_state["messages_sha256"] == new_state["messages_sha256"]
        and revision > old_revision,
        **pair,
        revision=revision,
        sha256=snapshot_sha256(restored),
    )


async def trace(store, folder, notebook, docs, report, phase, *, repair=False):
    run_id = folder.name + "-" + uuid4().hex[:12]
    secret = "Synthetic" + uuid4().hex[:12]
    seed = f"Өвлийн тэмдэглэл 🧭\r\n{MARKER}\r\npassword={secret}"
    # The answer changes every run. The question contains neither these words nor the code.
    sentence = f"The winter observatory keeps its spare compass in the amber cedar drawer marked {uuid4().hex[:10]}."
    origin = folder / "rollout-field-log.jsonl"
    capture(store.path, origin, run_id, seed)
    chat = store.connection.execute("SELECT id FROM conversations").fetchone()[0]
    await phase("seed")
    seeded = await publication_cycle(store, report, "seed_publication", docs)
    first = source_pair(store)
    capture(store.path, origin, run_id, seed, sentence)
    capture(store.path, origin, run_id, seed, sentence)
    rows = store.connection.execute(
        "SELECT id,text FROM messages WHERE conversation_id=? ORDER BY sequence", (chat,)
    ).fetchall()
    archived = len(rows) == 2 and [r["text"] for r in rows] == [seed, sentence]
    report.verify(
        "origin_sqlite",
        archived,
        chat_id=chat,
        message_id=rows[-1]["id"],
        origin_sha256=snapshot_sha256(sentence),
        sqlite_sha256=snapshot_sha256(rows[-1]["text"]),
    )
    await phase("append")
    published = await publication_cycle(store, report, "append_publication", docs)
    pair = source_pair(store)
    row = store.connection.execute(
        """SELECT o.snapshot,o.revision,p.written_hash FROM google_docs_parts p
        JOIN outbound_revisions o USING(segment_id) WHERE p.part=0 ORDER BY o.revision DESC LIMIT 1"""
    ).fetchone()
    if row and pair.get("document_id"):
        parts = split_document(bytes(row["snapshot"]).decode())
        part = parts[0]
        hash_ = snapshot_sha256(part)
        expected_hash = snapshot_sha256(f"{INDEX_RECEIPT_PREFIX}{hash_[:24]}\n{part}")
        await phase("cold_reread")
        remote = await docs.call("check", id=pair["document_id"])
        exact = (
            len(parts) == 1
            and part.count(sentence) == 1
            and secret not in part
            and remote.get("hash") == expected_hash
            and row["written_hash"] == hash_
        )
        report.verify(
            "reopened_document",
            exact,
            **pair,
            revision=row["revision"],
            expected_sha256=expected_hash,
            reopened_sha256=remote.get("hash"),
            occurrences_in_verified_text=part.count(sentence) if exact else None,
        )
    else:
        report.record("reopened_document", "BLOCKED", reason="no_written_document")
    if published:
        await phase("ask")
        begin = time.monotonic()
        reply = await MemoryApplication(store.path).call("memory_ask", {"question": QUESTION})
        refs = reply.get("sources", [])
        inventory = {s["id"]: s for s in await notebook.list_sources()}
        attested = any(
            ref.get("source_id") == pair.get("source_id")
            and ref.get("segment_id") == pair.get("segment_id")
            and ref.get("revision") == row["revision"]
            and ref.get("stale") is False
            and sentence in (ref.get("cited_text") or "")
            and ref.get("conversation_id") == chat
            and rows[-1]["id"] in ref.get("message_ids", [])
            for ref in refs
        )
        code = reply.get("error", {}).get("code")
        good = (
            attested
            and sentence in reply.get("answer", "")
            and inventory.get(pair["source_id"], {}).get("drive_document_id") == pair["document_id"]
        )
        report.record(
            "memory_ask",
            "PASS" if good else ("BLOCKED" if code in BLOCKERS else "FAIL"),
            reason=code,
            exact_cited_message=attested,
            references=len(refs),
            citation_hashes=[snapshot_sha256(r.get("cited_text") or "") for r in refs],
            answer_contains_sentence=sentence in reply.get("answer", ""),
            **pair,
            seconds=round(time.monotonic() - begin, 2),
        )
    else:
        report.record("memory_ask", "BLOCKED", reason="no_verified_publication")
    if repair:
        if published and report.checks["reopened_document"]["status"] == "PASS":
            await check_mirror_repair(store, docs, report, phase, pair, sentence)
        else:
            report.record("mirror_repair", "BLOCKED", reason="no_verified_publication")
    await phase("unchanged")
    if published:
        await publication_cycle(store, report, "unchanged_publication", docs)
    else:
        report.record("unchanged_publication", "BLOCKED", reason="no_verified_publication")
    if pair.get("document_id"):
        await docs.call("close", id=pair["document_id"])
    return seeded, published, bool(first) and first == pair == source_pair(store)


async def run(args):
    folder = args.output.expanduser().absolute()
    # Exclusive creation rejects retained archives, symlinks and concurrent runs.
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = Evidence(folder)
    notebook = NotebookLMAdapter(args.notebook_id, args.storage)
    db = folder / "audit.sqlite"
    docs = BackgroundDocsTransport(args.storage)
    metering = False
    async with docs:
        return await run_audit(args, folder, report, notebook, db, docs, metering)


async def run_audit(args, folder, report, notebook, db, docs, metering):
    with report.measure_notebook(), Store(db) as store:
        store.set_setting("tui_auto_sync", False)
        try:
            if await notebook.list_sources():
                report.record("setup", "BLOCKED", reason="notebook_not_empty")
                return False
            if (await docs.call("ping")).get("status") != "ready":
                report.record("setup", "BLOCKED", reason="docs_web_unavailable")
                return False
            store.set_setting("notebook_id", args.notebook_id)
            if args.storage:
                store.set_setting("storage_path", str(args.storage.expanduser().resolve()))
            enable_web_docs(store, BACKGROUND_SESSION)
            await browser_audit(docs, "start", marker=MARKER)
            metering = True
            report.record(
                "setup", "PASS", empty_notebook=True, auto=False, notebook_id=args.notebook_id
            )

            async def phase(name):
                report.phase = name
                await browser_audit(docs, "phase", phase=name)

            seeded, published, stable = await trace(
                store, folder, notebook, docs, report, phase, repair=args.repair
            )
            traffic = await browser_audit(docs, "report")
            append = traffic.get("append", {})
            idle = traffic.get("unchanged", {})
            if seeded and published:
                report.verify(
                    "incremental_save",
                    append.get("saves", 0) > 0
                    and 0 < append.get("saveBytes", 0)
                    and not append.get("unknownSaveBodies", 0)
                    and not append.get("unchangedMarkerSent", True),
                    counts=append,
                )
                report.verify(
                    "no_change",
                    stable
                    and idle.get("saves") == 0
                    and idle.get("saveBytes") == 0
                    and report.checks["unchanged_publication"]["status"] == "PASS",
                    stable_ids=stable,
                )
            else:
                for name in ("incremental_save", "no_change"):
                    report.record(name, "BLOCKED", reason="no_verified_publication")
        except Exception as exc:
            report.record(
                "interrupted",
                "BLOCKED" if failure_code(exc) in BLOCKERS else "FAIL",
                phase=report.phase,
                reason=failure_code(exc),
                exception=type(exc).__name__,
            )
        finally:
            if metering:
                try:
                    traffic = await browser_audit(docs, "report", stop=True)
                    write_private_text(
                        folder / "browser-bytes.json", json.dumps(traffic, indent=2) + "\n"
                    )
                except Exception as exc:
                    report.record("browser_meter", "FAIL", reason=failure_code(exc))
            await notebook.close()
            check_restore(store, folder, report)
    return all(c["status"] == "PASS" for c in report.checks.values())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True, help="New private directory")
    p.add_argument("--notebook-id", required=True, help="Empty, dedicated live test notebook")
    p.add_argument(
        "--storage", type=Path, help="Existing NotebookLM SDK login used by the test account"
    )
    p.add_argument(
        "--repair", action="store_true", help="Verify repair of the synthetic Google Doc"
    )
    args = p.parse_args()
    try:
        return 0 if asyncio.run(run(args)) else 1
    except FileExistsError:
        p.exit(2, "Test Mode needs a new output directory.\n")


if __name__ == "__main__":
    sys.exit(main())

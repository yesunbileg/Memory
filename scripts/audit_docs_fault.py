"""Live fault injection limited to a newly created synthetic Docs document."""

import argparse
import asyncio
import json
import re
import tempfile
from pathlib import Path
from uuid import uuid4

from audit_docs import capture

from ai_memory.privacy import snapshot_sha256
from ai_memory.private_io import write_private_text
from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store
from ai_memory.web_docs_journal import DocumentJournal
from ai_memory.web_docs_publication import DocsPublisher, enable_web_docs
from ai_memory.web_docs_transport import DocsWebError, WebDocsTransport

SEED = "Synthetic network recovery audit. Монгол 🧭"
BEFORE = SEED + "\nRequest interrupted before server commit."
AFTER = BEFORE + "\nRequest interrupted after server commit."
STEPS = {"before": (SEED, BEFORE), "after": (BEFORE, AFTER)}


async def route_control(session, action, document, **kwargs):
    params = {"action": action, "id": document, **kwargs}
    js = Path(__file__).with_suffix(".js").read_text()
    with tempfile.TemporaryDirectory(prefix="memory-fault-") as temp:
        path = Path(temp) / "route.js"
        write_private_text(path, "const faultInput = " + json.dumps(params) + ";\n" + js)
        proc = await asyncio.create_subprocess_exec(
            "playwriter",
            "-s",
            str(session),
            "--timeout",
            "12000",
            "-f",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
    match = re.search(rb"(?m)^(?:\[log\] )?FAULT_RESULT=(\{[^\r\n]+\})", out)
    if not match or proc.returncode:
        raise RuntimeError("fault_route_unavailable")
    return json.loads(match[1])


async def run(args):
    folder = args.output.resolve()
    docs = WebDocsTransport(args.session)
    path = folder / "faults.json"
    if args.step == "init":
        folder.mkdir(mode=0o700, parents=True, exist_ok=False)
        db = folder / "audit.sqlite"
        with Store(db) as store:
            store.set_setting("tui_auto_sync", False)
            store.set_setting("notebook_id", "synthetic-docs-fault-only")
            enable_web_docs(store, args.session)
        capture(db, folder / "synthetic.jsonl", uuid4().hex, SEED)
        with Store(db) as store:
            chat = store.connection.execute("SELECT id FROM conversations").fetchone()[0]
            store.approve(chat)
            SegmentBuilder(store).build()
            segment = store.connection.execute("SELECT id FROM segments").fetchone()[0]
            title = f"AI Memory {store.archive_instance_id()[:12]} {segment} part 1"
            with store.connection:
                store.connection.execute(
                    "INSERT INTO google_docs_parts(segment_id,part,creating) VALUES(?,0,1)",
                    (segment,),
                )
            doc = await docs.create(title)
            with store.connection:
                store.connection.execute(
                    "UPDATE google_docs_parts SET document_id=? WHERE segment_id=? AND part=0",
                    (doc, segment),
                )
            write_private_text(
                path,
                json.dumps(
                    {
                        "session": args.session,
                        "doc": doc,
                        "title": title,
                        "segment": segment,
                        "checks": {},
                    },
                    indent=2,
                ),
            )
            print("isolated test document created")
        return
    result = json.loads(path.read_text())
    assert result["session"] == args.session
    doc, title, segment = result["doc"], result["title"], result["segment"]

    def record(name, **values):
        result["checks"][name] = values
        write_private_text(path, json.dumps(result, indent=2) + "\n")
        print(name, json.dumps(values), flush=True)

    with Store(folder / "audit.sqlite") as store:
        journal = DocumentJournal(store, docs, doc)
        publisher = DocsPublisher(store, None, docs, lambda *_: None)
        if args.step == "title":
            await route_control(args.session, "arm", doc, mode="title", title=title)
            try:
                try:
                    await publisher._document(segment, 0, title)
                    error = None
                except DocsWebError as exc:
                    error = exc.code
            finally:
                fault = await route_control(args.session, "clear", doc)
            part = store.connection.execute(
                "SELECT document_id,creating,source_id FROM google_docs_parts"
            ).fetchone()
            record(
                "title_interrupted",
                status="PASS"
                if error == "docs_web_title_unconfirmed"
                and fault["matches"] > 0
                and tuple(part) == (doc, 1, None)
                and journal._load() == {"text": ""}
                else "FAIL",
                error=error,
                fault=fault,
                id_retained=part["document_id"] == doc,
                source_not_linked=part["source_id"] is None,
            )
        elif args.step == "title_retry":
            before = await docs.call("check", id=doc)
            got = await publisher._document(segment, 0, title)
            try:
                found = await docs.call("find", title=title)
            except DocsWebError as exc:
                found = {"error": exc.code}
            row = store.connection.execute(
                "SELECT document_id,creating,source_id FROM google_docs_parts"
            ).fetchone()
            record(
                "title_retry",
                status="PASS"
                if before["hash"] == snapshot_sha256("")
                and got == doc
                and tuple(row) == (doc, 0, None)
                else "FAIL",
                same_id=got == doc,
                empty_before_write=before["hash"] == snapshot_sha256(""),
                exact_matching_titles=len(found["ids"]) if "ids" in found else None,
                title_search_error=found.get("error"),
            )
        elif args.step == "seed":
            count = await journal.sync(SEED)
            record("seed", status="PASS" if count > 0 else "FAIL", bytes=count)
        elif args.step in ("before", "after"):
            old, new = STEPS[args.step]
            assert journal._load() == {"text": old}
            assert (await docs.call("prepare", id=doc, known=snapshot_sha256(old)))[
                "status"
            ] == "ready"
            await route_control(args.session, "arm", doc, mode=args.step)
            try:
                try:
                    await journal.sync(new)
                    error = None
                except DocsWebError as exc:
                    error = exc.code
            finally:
                fault = await route_control(args.session, "clear", doc)
            pending = journal._load()
            expected = snapshot_sha256(old if args.step == "before" else new)
            # Remote verification is a separate process invocation if necessary.
            record(
                args.step + "_interrupted",
                status="PASS"
                if error == "docs_web_uncertain"
                and fault["matches"] == 1
                and pending.get("pending", {}).get("text") == new
                and (args.step == "before" or fault["serverResponses"] == [200])
                else "FAIL",
                error=error,
                fault=fault,
                pending_durable=pending.get("pending", {}).get("text") == new,
                expected_hash=expected,
            )
        elif args.step in ("before_verify", "after_verify"):
            mode = args.step.split("_")[0]
            old, new = STEPS[mode]
            remote = await docs.call("check", id=doc)
            expected = snapshot_sha256(old if mode == "before" else new)
            record(
                mode + "_remote",
                status="PASS" if remote["hash"] == expected else "FAIL",
                expected_hash=expected,
                remote_hash=remote["hash"],
                revision=remote["revision"],
            )
        elif args.step in ("before_retry", "after_retry"):
            mode = args.step.split("_")[0]
            old, new = STEPS[mode]
            count = await journal.sync(new)
            record(
                mode + "_retry",
                status="PASS"
                if journal._load() == {"text": new}
                and ((count > 0) if mode == "before" else count == 0)
                else "FAIL",
                retry_bytes=count,
            )
        elif args.step == "final":
            remote = await docs.call("check", id=doc)
            record(
                "final",
                status="PASS"
                if remote["hash"] == snapshot_sha256(AFTER) and journal._load() == {"text": AFTER}
                else "FAIL",
                remote_hash=remote["hash"],
                expected_hash=snapshot_sha256(AFTER),
                revision=remote["revision"],
            )
            await docs.call("close", id=doc)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--session", required=True, type=int)
    parser.add_argument(
        "step",
        choices=[
            "init",
            "title",
            "title_retry",
            "seed",
            "before",
            "before_verify",
            "before_retry",
            "after",
            "after_verify",
            "after_retry",
            "final",
        ],
    )
    asyncio.run(run(parser.parse_args()))

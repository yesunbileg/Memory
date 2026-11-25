"""Isolated, resumable Google Docs live benchmark and fault audit.

Use only newly created synthetic documents in the dedicated audit session.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

from audit_docs import browser_audit

from ai_memory.privacy import snapshot_sha256
from ai_memory.private_io import write_private_text
from ai_memory.store import Store
from ai_memory.web_docs_journal import DocumentJournal
from ai_memory.web_docs_transport import DocsWebError, WebDocsTransport

MARKER = "TESTMODEUNCHANGEDSENTINEL"
ADDITION = "\nThe verified web sync compass code is ArcticMallow for sailors."
assert len(ADDITION.split()) == 10


def base(kind):
    prefix = MARKER + " "
    if kind == "small":
        return prefix + "west " * 998 + "west"
    # Leave ample room for the ten-word append under the UTF-16 ceiling.
    return prefix + "west " * ((999_000 - len(prefix)) // 5)


def save(folder, state):
    write_private_text(folder / "benchmark.json", json.dumps(state, indent=2) + "\n")


async def run(args):
    folder = args.output.resolve()
    if args.action == "init":
        folder.mkdir(mode=0o700, parents=True, exist_ok=False)
        state = {"session": args.session, "run": uuid4().hex[:12], "documents": {}}
        save(folder, state)
    else:
        state = json.loads((folder / "benchmark.json").read_text())
        assert state["session"] == args.session
    docs = WebDocsTransport(args.session)
    if args.action == "init":
        assert (await docs.call("ping"))["status"] == "ready"
        return
    with Store(folder / "audit.sqlite") as store:
        if args.action == "create":
            assert args.kind not in state["documents"]
            title = f"AI Memory Test Benchmark {state['run']} {args.kind}"
            doc = await docs.create(title)
            state["documents"][args.kind] = {"id": doc, "title": title, "seed_length": 0}
            save(folder, state)
            await docs.confirm_title(doc, title)
            print("created", args.kind, flush=True)
            return
        entry = state["documents"][args.kind]
        doc = entry["id"]
        journal = DocumentJournal(store, docs, doc)
        content = base(args.kind)
        if args.action == "seed":
            end = min(len(content), entry["seed_length"] + args.chunk)
            begin = time.monotonic()
            await journal.sync(content[:end])
            entry["seed_length"] = end
            entry.setdefault("seed_steps", []).append(
                {
                    "length": end,
                    "confirmed_bytes": journal.write_payload_bytes,
                    "seconds": round(time.monotonic() - begin, 2),
                }
            )
            save(folder, state)
            print("seed", args.kind, end, "/", len(content), flush=True)
            return
        assert entry["seed_length"] == len(content)
        if args.action == "append":
            assert "append" not in entry
            await docs.call("close", id=doc)
            await browser_audit(args.session, "start", marker=MARKER)
            try:
                await browser_audit(args.session, "phase", phase="cold_open")
                prepared = await docs.call("prepare", id=doc, known=snapshot_sha256(content))
                assert prepared.get("status") == "ready"
                await browser_audit(args.session, "phase", phase="append")
                begin = time.monotonic()
                count = await journal.sync(content + ADDITION)
                seconds = round(time.monotonic() - begin, 2)
                await browser_audit(args.session, "phase", phase="unchanged")
                unchanged = await journal.sync(content + ADDITION)
            finally:
                traffic = await browser_audit(args.session, "report", stop=True)
            append = traffic.get("append", {})
            idle = traffic.get("unchanged", {})
            entry["append"] = {
                "status": "PASS"
                if 0 < count <= 2048
                and append.get("saves") == 1
                and append.get("saveBytes") == count
                and not append.get("unchangedMarkerSent")
                and unchanged == 0
                and idle.get("saves", 0) == 0
                else "FAIL",
                "bytes": count,
                "seconds": seconds,
                "unchanged_bytes": unchanged,
                "traffic": traffic,
            }
            save(folder, state)
            print(json.dumps(entry["append"]), flush=True)
            return
        if args.action == "verify":
            expected = snapshot_sha256(content + ADDITION)
            actual = await docs.call("check", id=doc)
            entry["reopened"] = {
                "status": "PASS" if actual.get("hash") == expected else "FAIL",
                "expected_hash": expected,
                "remote_hash": actual.get("hash"),
                "revision": actual.get("revision"),
            }
            save(folder, state)
            print(json.dumps(entry["reopened"]), flush=True)
            return
        if args.action == "close":
            await docs.call("close", id=doc)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--session", type=int, required=True)
    p.add_argument("action", choices=["init", "create", "seed", "append", "verify", "close"])
    p.add_argument("--kind", choices=["small", "large"], default="small")
    p.add_argument("--chunk", type=int, default=128_000)
    args = p.parse_args()
    try:
        asyncio.run(run(args))
    except DocsWebError as error:
        print("BLOCKED", error.code)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()

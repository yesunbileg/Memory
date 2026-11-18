"""Mongolian, synthetic-only workflow through real CLI and MCP entry points."""

import json
import os
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ai_memory.archive_files import workspace_for
from ai_memory.segments import SegmentBuilder
from ai_memory.store import Store
from ai_memory.sync import SyncEngine

ORIGINAL = (
    " \tСайн байна уу, санах ойн шийдвэр 🦦\r\n"
    "```python\r\nprint('Монгол')\r```\r\n"
    "password = 'SyntheticOnlyPass921!'\r\n  "
)
EDITED = ORIGINAL.replace("санах ойн шийдвэр", "шинэчилсэн шийдвэр")
ANSWER = "Эх хувийг SQLite-д хадгална. 🗂️"
QUESTION = "Өмнөх шийдвэрээр эх хувийг хаана хадгалах вэ?"
FOREIGN_QUESTION = "Хамааралгүй эх сурвалжийн хариулт байна уу?"
FOREIGN_ANSWER = "TEST_ONLY_FOREIGN_ANSWER"
FOREIGN_PASSAGE = "TEST_ONLY_FOREIGN_PRIVATE_PASSAGE"


class FakeNotebook:
    """Durable synthetic provider state shared across restarted clients."""

    def __init__(self, path, *, lose_upload_reply=False):
        self.path = Path(path)
        self.lose_upload_reply = lose_upload_reply

    def read(self):
        if not self.path.exists():
            return {"uploads": 0, "sources": {}, "questions": []}
        return json.loads(self.path.read_bytes())

    def write(self, state):
        self.path.write_bytes(json.dumps(state, ensure_ascii=False).encode())

    async def list_sources(self):
        return [
            {"id": source_id, "title": value["title"], "ready": value["ready"]}
            for source_id, value in self.read()["sources"].items()
        ]

    async def upload(self, path, title):
        state = self.read()
        state["uploads"] += 1
        source_id = f"synthetic-source-{state['uploads']}"
        state["sources"][source_id] = {
            "title": title,
            "ready": False,
            "text": path.read_bytes().decode(),
        }
        self.write(state)
        if self.lose_upload_reply:
            raise TimeoutError("Fake response lost after remote create")
        return source_id

    async def wait_ready(self, source_id):
        state = self.read()
        state["sources"][source_id]["ready"] = True
        self.write(state)

    async def get_source_text(self, source_id):
        return self.read()["sources"][source_id]["text"]

    async def delete(self, source_id):
        state = self.read()
        state["sources"].pop(source_id)
        self.write(state)

    async def ask(self, question, source_ids=None):
        state = self.read()
        state["questions"].append([question, source_ids])
        self.write(state)
        assert question in {QUESTION, FOREIGN_QUESTION}
        assert source_ids and all(value in state["sources"] for value in source_ids)
        reference = {"source_id": source_ids[0], "citation_number": 1, "cited_text": ANSWER}
        if question == FOREIGN_QUESTION:
            return {
                "answer": f"{FOREIGN_ANSWER} [1] [2]",
                "references": [
                    reference,
                    {
                        "source_id": "unrelated-notebook-source",
                        "citation_number": 2,
                        "cited_text": FOREIGN_PASSAGE,
                    },
                ],
            }
        return {"answer": f"{ANSWER} [1]", "references": [reference]}

    async def close(self):
        pass


def write_rollout(path, *, text=ORIGINAL, changed_on=1):
    created_at = "2026-01-01T00:00:00Z"
    updated_at = f"2026-01-{changed_on:02}T00:00:01Z"
    records = [
        {"type": "session_meta", "payload": {"id": "mongolian-e2e", "timestamp": created_at}},
        {
            "type": "response_item",
            "timestamp": updated_at,
            "payload": {
                "type": "message",
                "id": "turn-user",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:02Z",
            "payload": {
                "type": "message",
                "id": "turn-assistant",
                "role": "assistant",
                "content": [{"type": "output_text", "text": ANSWER}],
            },
        },
    ]
    path.write_bytes(
        b"\n".join(json.dumps(r, ensure_ascii=False).encode() for r in records) + b"\n"
    )


def cli(db, *arguments, ok=True):
    result = subprocess.run(
        [sys.executable, "-m", "ai_memory", "--db", str(db), *map(str, arguments)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == (0 if ok else 1), (result.stdout, result.stderr)
    return json.loads(result.stdout)


def rows_by_account(db):
    with Store(db) as store:
        return {
            row["account"]: store.get_conversation(row["id"])
            for row in store.connection.execute("SELECT id,account FROM conversations")
        }


def assert_original_text(conversation, user_text=ORIGINAL):
    assert [m["text"].encode() for m in conversation["messages"]] == [
        user_text.encode(),
        ANSWER.encode(),
    ]
    assert {m["native_id"] for m in conversation["messages"]} == {"turn-user", "turn-assistant"}


def decode_mcp(result):
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


async def verify_mcp_scope(db, remote_path, source_id):
    # Run the real CLI/SDK protocol in another process. The only substitution
    # is the provider adapter, constructed inside this synthetic test's child.
    bootstrap = (
        "import sys; from pathlib import Path; "
        "from test_memory_audit_e2e import FakeNotebook; "
        "from ai_memory import gateway, cli; "
        "gateway.NotebookLMAdapter = lambda *a, **kw: FakeNotebook(Path(sys.argv[1])); "
        "raise SystemExit(cli.main(sys.argv[2:]))"
    )
    test_dir = str(Path(__file__).resolve().parent)
    pythonpath = os.pathsep.join(filter(None, [test_dir, os.environ.get("PYTHONPATH")]))
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-c", bootstrap, str(remote_path), "--db", str(db), "mcp"],
        env={"PYTHONPATH": pythonpath},
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            assert {tool.name for tool in (await client.list_tools()).tools} == {
                "memory_ask",
                "memory_status",
            }
            accepted = await client.call_tool("memory_ask", {"question": QUESTION})
            assert not accepted.isError
            response = decode_mcp(accepted)
            assert ANSWER in response["answer"]
            assert [source["source_id"] for source in response["sources"]] == [source_id]

            rejected = await client.call_tool("memory_ask", {"question": FOREIGN_QUESTION})
            assert rejected.isError
            assert decode_mcp(rejected)["error"]["code"] == "unverified_sources"
            assert "answer" not in decode_mcp(rejected)
            wire = rejected.model_dump_json()
            assert FOREIGN_ANSWER not in wire and FOREIGN_PASSAGE not in wire

    assert FakeNotebook(remote_path).read()["questions"] == [
        [QUESTION, [source_id]],
        [FOREIGN_QUESTION, [source_id]],
    ]


def archive_evidence(db):
    with Store(db) as store:
        return {
            table: [dict(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
            for table in (
                "conversations",
                "messages",
                "checkpoints",
                "segments",
                "source_map",
                "sync_jobs",
                "source_receipts",
                "outbound_revisions",
                "registered_sources",
            )
        }


def account_checkpoint(db, account):
    with Store(db) as store:
        return {
            row["key"]: row["value"]
            for row in store.connection.execute("SELECT key,value FROM checkpoints")
            if f":{account}:" in row["key"]
        }


def requeue_after_deadline(store):
    # Advance the persisted retry clock, without a 30-second wall-clock sleep.
    with store.connection:
        store.connection.execute("UPDATE sync_jobs SET next_attempt=0 WHERE next_attempt>0")


async def test_mongolian_upload_restarts_and_limits_stdio_citations(tmp_path):
    db = tmp_path / "memory.sqlite"
    rollout = tmp_path / "rollout-mongolian.jsonl"
    remote_path = tmp_path / "remote.json"
    snapshot_dir = workspace_for(db) / "sync"
    write_rollout(rollout)
    cli(db, "init", "--notebook-id", "synthetic-test-notebook")
    cli(db, "collect", "codex", rollout, "--account", "alpha")
    cli(db, "collect", "codex", rollout, "--account", "beta")
    alpha = rows_by_account(db)["alpha"]
    cli(db, "approve", alpha["id"])
    expected_snapshots = cli(db, "preview", alpha["id"])["segments"]
    assert len(expected_snapshots) == 1
    expected_outbound = expected_snapshots[0]["text"].encode()

    first_client = FakeNotebook(remote_path, lose_upload_reply=True)
    with Store(db) as store:
        interrupted = await SyncEngine(
            store, SegmentBuilder(store), first_client, snapshot_dir
        ).run_once()
        assert interrupted["blocked"], interrupted

    remote = first_client.read()
    assert remote["uploads"] == 1
    assert len(remote["sources"]) == 1
    source_id, remote_source = next(iter(remote["sources"].items()))
    assert not remote_source["ready"]
    outbound = remote_source["text"].encode()
    assert (
        ORIGINAL.replace("SyntheticOnlyPass921!", "*" * len("SyntheticOnlyPass921!")).encode()
        in outbound
    )
    assert b"SyntheticOnlyPass921!" not in outbound
    assert outbound == expected_outbound
    assert any(outbound == path.read_bytes() for path in snapshot_dir.glob("*.md"))
    assert_original_text(rows_by_account(db)["alpha"])

    pending_archive = tmp_path / "restored-pending" / "memory.sqlite"
    pending_backup = tmp_path / "pending.sqlite"
    pending_evidence = archive_evidence(db)
    assert pending_evidence["sync_jobs"]
    cli(db, "backup", pending_backup)
    cli(pending_archive, "restore", pending_backup)
    assert archive_evidence(pending_archive) == pending_evidence

    # All local objects are new, while the fake provider remembers its operation.
    with Store(pending_archive) as restarted:
        requeue_after_deadline(restarted)
        result = await SyncEngine(
            restarted,
            SegmentBuilder(restarted),
            FakeNotebook(remote_path),
            workspace_for(pending_archive) / "sync",
        ).run_once()
        assert not result["blocked"], result
    remote = FakeNotebook(remote_path).read()
    assert remote["uploads"] == 1
    assert list(remote["sources"]) == [source_id]
    assert remote["sources"][source_id]["ready"] is True
    evidence = archive_evidence(pending_archive)
    assert [mapping["source_id"] for mapping in evidence["source_map"]] == [source_id]
    assert evidence["source_receipts"][0]["sha256"] == expected_snapshots[0]["sha256"]
    finished_backup = tmp_path / "finished.sqlite"
    finished_archive = tmp_path / "restored-finished" / "memory.sqlite"
    cli(pending_archive, "backup", finished_backup)
    cli(finished_archive, "restore", finished_backup)
    assert archive_evidence(finished_archive) == evidence
    await verify_mcp_scope(finished_archive, remote_path, source_id)


def test_mongolian_cli_capture_and_approval_account_isolation(tmp_path):
    db = tmp_path / "memory.sqlite"
    rollout = tmp_path / "rollout-mongolian.jsonl"
    write_rollout(rollout)
    cli(db, "init", "--notebook-id", "synthetic-test-notebook")

    cli(db, "collect", "codex", rollout, "--account", "alpha")
    alpha = rows_by_account(db)["alpha"]
    alpha_checkpoint = account_checkpoint(db, "alpha")
    assert alpha_checkpoint
    assert not account_checkpoint(db, "beta")
    cli(db, "collect", "codex", rollout, "--account", "alpha")
    assert rows_by_account(db)["alpha"] == alpha
    assert_original_text(alpha)

    failure = cli(db, "approve", alpha["id"], "CX-DOES-NOT-EXIST", ok=False)
    assert "error" in failure
    assert rows_by_account(db)["alpha"]["consent"] is False
    cli(db, "approve", alpha["id"], alpha["id"])
    assert rows_by_account(db)["alpha"]["consent"] is True

    cli(db, "collect", "codex", rollout, "--account", "beta")
    accounts = rows_by_account(db)
    beta = accounts["beta"]
    beta_checkpoint = account_checkpoint(db, "beta")
    assert beta_checkpoint and beta_checkpoint.keys().isdisjoint(alpha_checkpoint)
    assert_original_text(beta)
    assert beta["id"] != alpha["id"]
    assert {m["id"] for m in beta["messages"]}.isdisjoint(m["id"] for m in alpha["messages"])
    assert beta["consent"] is False

    write_rollout(rollout, text=EDITED, changed_on=2)
    cli(db, "collect", "codex", rollout, "--account", "alpha")
    accounts = rows_by_account(db)
    assert accounts["beta"] == beta
    assert account_checkpoint(db, "beta") == beta_checkpoint
    assert_original_text(accounts["alpha"], EDITED)
    assert accounts["alpha"]["consent"] is True
    assert accounts["alpha"]["revision"] > alpha["revision"]
    assert [m["id"] for m in accounts["alpha"]["messages"]] == [m["id"] for m in alpha["messages"]]

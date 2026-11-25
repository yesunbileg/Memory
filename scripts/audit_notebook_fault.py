"""Measure a live, isolated NotebookLM Drive-link response interruption."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
from audit_docs import Evidence
from notebooklm.rpc.types import RPCMethod

from ai_memory.notebook import NotebookLMAdapter
from ai_memory.store import Store
from ai_memory.web_docs_publication import DocsPublisher

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def run(args):
    from mem_backend import Backend

    original = json.loads((args.original / "evidence.json").read_text())
    test_id = original["checks"]["setup"]["notebook_id"]
    initial = original["checks"]["reopened_document"]
    with Backend()._archive() as (conn, _):
        view = Backend._view(conn)
        assert view.get_setting("notebook_id") != test_id
        storage = view.get_setting("storage_path")
    fault = json.loads((args.fault / "faults.json").read_text())
    assert fault["checks"]["final"]["status"] == "PASS"
    doc, title = fault["doc"], fault["title"]
    assert doc != initial["document_id"]
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = Evidence(args.output)
    notebook = NotebookLMAdapter(test_id, storage)
    with evidence.measure_notebook(), Store(args.fault / "audit.sqlite", existing=True) as store:
        try:
            inventory = await notebook.list_sources()
            assert len(inventory) == 1 and inventory[0]["id"] == initial["source_id"]
            assert inventory[0].get("drive_document_id") == initial["document_id"]
            assert not any(item.get("drive_document_id") == doc for item in inventory)
            evidence.record("isolated_notebook", "PASS", initial_sources=len(inventory))
            store.set_setting("notebook_id", test_id)
            publisher = DocsPublisher(store, notebook, None, lambda *_: None)
            row = publisher._part(fault["segment"], 0)
            assert not row["source_id"] and not row["linking"]
            send = httpx.AsyncClient.send
            intercepted = {"calls": 0, "forwarded": 0, "http_status": None, "bytes": 0}

            async def interrupted(client, request, **kwargs):
                ids = (request.url.params.get("rpcids") or "").split(",")
                if RPCMethod.ADD_SOURCE.value not in ids:
                    return await send(client, request, **kwargs)
                # Fail closed: this audit may only send the one synthetic link.
                assert doc.encode() in request.content and test_id.encode() in request.content
                intercepted["calls"] += 1
                if intercepted["calls"] > 1:
                    raise AssertionError("ambiguous add was replayed")
                response = await send(client, request, **kwargs)
                intercepted["forwarded"] += 1
                intercepted["http_status"] = response.status_code
                await response.aread()
                intercepted["bytes"] = response.num_bytes_downloaded
                await response.aclose()
                raise httpx.ReadError("Synthetic response loss after commit", request=request)

            evidence.phase = "link_response_lost"
            try:
                with patch.object(httpx.AsyncClient, "send", interrupted):
                    source, newly = await publisher._source(row, doc, title, inventory)
            except Exception as exc:
                evidence.record(
                    "link_response_lost",
                    "BLOCKED",
                    error=type(exc).__name__,
                    reason=getattr(exc, "code", None),
                    **intercepted,
                )
                return
            evidence.phase = "inventory_verify"
            final = await notebook.list_sources()
            matches = [item for item in final if item.get("drive_document_id") == doc]
            current = publisher._part(fault["segment"], 0)
            evidence.verify(
                "link_response_lost",
                intercepted["calls"] == intercepted["forwarded"] == 1
                and intercepted["http_status"] == 200
                and len(matches) == 1
                and source == matches[0]["id"] == current["source_id"]
                and newly
                and current["linking"] == 0
                and len(final) == 2,
                **intercepted,
                matching_sources=len(matches),
                source_id=source,
                document_id=doc,
                durable_source=current["source_id"] == source,
            )
            evidence.phase = "retry"
            again, newly = await publisher._source(current, doc, title, final)
            evidence.verify("link_retry", again == source and not newly, stable_source_id=again)
        finally:
            await notebook.close()
            evidence.save()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--original", type=Path, required=True)
    p.add_argument("--fault", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    asyncio.run(run(p.parse_args()))

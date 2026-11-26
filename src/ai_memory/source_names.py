"""Readable NotebookLM labels for sources identified by the archive."""

import json
import re

from .cold_archive import markdown_segment
from .diagnostics import failure_code

ENABLED = "notebook_source_names"
_SEGMENT = re.compile(r"SEG-(\d{3,})\Z")
_LABEL = "source-display-name:"


def markdown_name(store, segment_id):
    number = _SEGMENT.fullmatch(segment_id)
    if not number:
        raise ValueError("Invalid segment ID")
    if markdown_segment(store, segment_id):
        return f"Архив {number[1]} · 30+ хоног"
    return f"Чатын багц {number[1]}"


def docs_name(segment_id, part):
    number = _SEGMENT.fullmatch(segment_id)
    if not number:
        raise ValueError("Invalid segment ID")
    name = f"Идэвхтэй чат {number[1]}"
    return f"{name} · үргэлжлэл {part + 1}" if part else name


def _saved_name(store, key):
    row = store.connection.execute("SELECT value FROM checkpoints WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _save_name(store, key, title):
    store.connection.execute(
        "INSERT OR REPLACE INTO checkpoints VALUES(?,?)",
        (key, json.dumps(title, ensure_ascii=False)),
    )


def submitted_markdown_name(store, job):
    return _saved_name(store, f"markdown-upload-name:{job['id']}") or job["title"]


def set_markdown_submission_name(store, job):
    if store.get_setting(ENABLED):
        _save_name(
            store,
            f"markdown-upload-name:{job['id']}",
            markdown_name(store, job["segment_id"]) + ".md",
        )


def drive_document_name(store, segment_id, part, legacy, *, new):
    """Keep a stable, archive-specific Drive name across uncertain creates."""
    key = f"docs-create-name:{segment_id}:{part}"
    if name := _saved_name(store, key):
        return name
    if new and store.get_setting(ENABLED):
        archive_id = store.archive_instance_id()[:12]
        name = f"{docs_name(segment_id, 0)} · хэсэг {part + 1} · {archive_id}"
        with store.connection:
            _save_name(store, key, name)
        return name
    return legacy


def record_verified_name(store, source_id, title):
    _save_name(store, _LABEL + source_id, title)


def matches_source_title(store, source_id, observed, original):
    if observed == original:
        return True
    return observed is not None and observed == _saved_name(store, _LABEL + source_id)


def _targets(store, inventory, *, include_legacy):
    from .web_docs_publication import DOCS_SETTING

    db = store.connection
    names = {}
    docs_ids = set()
    if store.get_setting(DOCS_SETTING):
        docs_ids = {
            row[0]
            for row in db.execute(
                "SELECT source_id FROM google_docs_parts WHERE source_id IS NOT NULL"
            )
        }
        for row in db.execute("""SELECT p.*,d.part_count FROM google_docs_parts p
            JOIN google_docs_publications d USING(segment_id)
            JOIN segments s ON s.id=p.segment_id
            JOIN source_receipts r ON r.source_id=p.source_id AND r.segment_id=p.segment_id
              AND r.revision=d.ready_revision
            WHERE s.state!='retired' AND p.part<d.part_count"""):
            source = inventory.get(row["source_id"])
            number = _SEGMENT.fullmatch(row["segment_id"])
            if not source or not number or source.get("drive_document_id") != row["document_id"]:
                continue
            names[source["id"]] = docs_name(row["segment_id"], row["part"])
    for row in db.execute("""SELECT m.* FROM source_map m JOIN segments s ON s.id=m.segment_id
        JOIN source_receipts r ON r.source_id=m.source_id AND r.segment_id=m.segment_id
          AND r.revision=m.revision
        JOIN sync_jobs j ON j.segment_id=m.segment_id AND j.revision=m.revision
          AND j.new_source_id=m.source_id AND j.stage='done'
        WHERE s.state!='retired'"""):
        source = inventory.get(row["source_id"])
        number = _SEGMENT.fullmatch(row["segment_id"])
        if not source or not number or source["id"] in docs_ids or source.get("drive_document_id"):
            continue
        names[source["id"]] = markdown_name(store, row["segment_id"])
    if include_legacy:
        # Name only the unique, abandoned copy from the Docs cutover. This is
        # a display label, never evidence of ownership or permission to delete.
        for row in db.execute("""SELECT segment_id,revision,title FROM sync_jobs
            WHERE stage='superseded' AND failure_phase='google_docs_cutover'
              AND new_source_id IS NULL"""):
            matches = [
                s
                for s in inventory.values()
                if s["title"] == row["title"]
                and s["id"] not in names
                and not s.get("drive_document_id")
            ]
            number = _SEGMENT.fullmatch(row["segment_id"])
            if len(matches) == 1 and number:
                names[matches[0]["id"]] = f"Хуучин хуулбар {number[1]} · r{row['revision']}"
    return names


async def apply_source_names(store, adapter, *, include_legacy=False):
    inventory = {source["id"]: source for source in await adapter.list_sources()}
    desired = _targets(store, inventory, include_legacy=include_legacy)
    changed = {sid: name for sid, name in desired.items() if inventory[sid]["title"] != name}
    failures = {}
    for sid, name in changed.items():
        try:
            await adapter.rename_source(sid, name)
        except Exception as error:
            failures[sid] = failure_code(error)
    observed = (
        {source["id"]: source for source in await adapter.list_sources()} if changed else inventory
    )
    verified = {
        sid: name for sid, name in desired.items() if observed.get(sid, {}).get("title") == name
    }
    with store.connection:
        store.connection.executemany(
            "INSERT OR REPLACE INTO checkpoints VALUES(?,?)",
            (
                (_LABEL + sid, json.dumps(name, ensure_ascii=False))
                for sid, name in verified.items()
            ),
        )
    return {
        "renamed": sum(sid in verified for sid in changed),
        "confirmed": len(verified),
        "failed": [
            failures.get(sid, "source_rename_unverified")
            for sid in desired.keys() - verified.keys()
        ],
    }

"""Preview masked outbound segments using a private local copy of the archive."""

import tempfile
from pathlib import Path

from .diagnostics import PublicInputError
from .privacy import MASKING_POLICY, find_sensitive, snapshot_sha256


def preview(store, key):
    """Build the same masked output as sync without mutating the live archive."""
    from .segments import SegmentBuilder
    from .store import Store

    if not store.connection.execute("SELECT 1 FROM conversations WHERE id=?", (key,)).fetchone():
        raise PublicInputError("Conversation does not exist")
    with (
        tempfile.TemporaryDirectory(prefix="ai-memory-preview-") as temp,
        Store(Path(temp) / "preview.sqlite") as sim,
    ):
        store.connection.backup(sim.connection)
        builder = SegmentBuilder(sim)
        builder.build()
        segments = []
        for row in sim.connection.execute(
            """SELECT DISTINCT s.id,s.revision FROM segments s
            JOIN segment_messages sm ON sm.segment_id=s.id JOIN messages m ON m.id=sm.message_id
            WHERE s.state!='retired' AND m.conversation_id=? ORDER BY s.id""",
            (key,),
        ):
            text = builder.render_outbound(row["id"])
            # A residual credential must not appear in the preview/error output.
            blocked = bool(find_sensitive(text))
            segments.append(
                {
                    "segment_id": row["id"],
                    "revision": row["revision"],
                    "masking_policy": MASKING_POLICY,
                    "sha256": snapshot_sha256(text),
                    "text": None if blocked else text,
                    "blocked": blocked,
                }
            )
    return {
        "conversation_id": key,
        "segments": segments,
        "note": "Local only. An edit or a new import can change these bytes.",
    }

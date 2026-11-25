"""Text changes accepted by the Google Docs web editor's save operation."""

from .web_text_delta import TextEdit


def editor_commands(edits: list[TextEdit]) -> list[dict]:
    """Encode UTF-16 edits against the original document, from end to start."""
    commands = []
    next_start = None
    for edit in edits:
        if not 0 <= edit.start <= edit.end or next_start is not None and edit.end > next_start:
            raise ValueError("Document edits overlap or are out of order")
        next_start = edit.start
        if edit.start < edit.end:
            # The editor's delete indices are one-based and inclusive.
            commands.append({"ty": "ds", "si": edit.start + 1, "ei": edit.end})
        if edit.text:
            commands.append({"ty": "is", "ibi": edit.start + 1, "s": edit.text})
    return [{"ty": "mlti", "mts": commands}] if commands else []


def confirmed_revision(acknowledgement: dict, previous: int) -> int | None:
    """Accept only the next revision without a concurrent-edit transformation."""
    expected = previous + 1
    metadata = acknowledgement.get("metadata")
    if not isinstance(metadata, dict):
        return None
    if (
        metadata.get("needsTransformOnClient") is False
        and metadata.get("serverRevision") == previous
        and acknowledgement.get("revisionRanges") == [[expected, expected]]
    ):
        return expected
    return None

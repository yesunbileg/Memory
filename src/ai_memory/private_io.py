"""Private, atomic text-file replacement without changing parent permissions."""

import os
import tempfile
from pathlib import Path


def write_private_text(path: Path, text: str) -> None:
    """Write UTF-8 text through a 0600 temporary file in the existing parent.

    The destination remains unchanged until the complete contents have been
    flushed and synced. Temporary files are removed if writing or replacing
    fails. Callers own creating the output directory, if needed.
    """
    path = Path(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        # mkstemp creates at most 0600 from the outset. Restore owner access if
        # the process umask is more restrictive, before any contents are written.
        os.fchmod(descriptor, 0o600)
        file = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
        descriptor = -1  # The file object now owns the descriptor.
        with file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)

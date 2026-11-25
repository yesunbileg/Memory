"""Calculate plain-text edits without placing unchanged text in an upload."""

from dataclasses import dataclass
from itertools import accumulate
from unicodedata import category

_MAX_DIFF_WORK = 1_000_000


@dataclass(frozen=True)
class TextEdit:
    start: int
    end: int
    text: str


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _common_prefix(before: str, after: str) -> int:
    for index, (old, new) in enumerate(zip(before, after, strict=False)):
        if old != new:
            return index
    return min(len(before), len(after))


def _unsafe_boundary(text: str, index: int) -> bool:
    return 0 < index < len(text) and (
        category(text[index]).startswith("M") or "\u200d" in text[index - 1 : index + 1]
    )


def _trim_edit(before: str, after: str, offset: int) -> TextEdit:
    prefix = _common_prefix(before, after)
    end = _common_prefix(before[prefix:][::-1], after[prefix:][::-1])
    while _unsafe_boundary(before, prefix) or _unsafe_boundary(after, prefix):
        prefix -= 1
    while _unsafe_boundary(before, len(before) - end) or _unsafe_boundary(after, len(after) - end):
        end -= 1
    old_end, new_end = len(before) - end, len(after) - end
    return TextEdit(
        offset + utf16_length(before[:prefix]),
        offset + utf16_length(before[:old_end]),
        after[prefix:new_end],
    )


def _myers_opcodes(old_lines: list[str], new_lines: list[str], max_distance: int):
    """Find line edits in O((N + M)D), stopping at the caller's work budget."""
    n, m = len(old_lines), len(new_lines)
    frontier, trace = {1: 0}, []
    for distance in range(max_distance + 1):
        trace.append(frontier.copy())
        for diagonal in range(-distance, distance + 1, 2):
            if diagonal == -distance or (
                diagonal != distance
                and frontier.get(diagonal - 1, -1) < frontier.get(diagonal + 1, -1)
            ):
                x = frontier.get(diagonal + 1, 0)
            else:
                x = frontier.get(diagonal - 1, 0) + 1
            y = x - diagonal
            while x < n and y < m and old_lines[x] == new_lines[y]:
                x += 1
                y += 1
            frontier[diagonal] = x
            if x < n or y < m:
                continue
            operations = []
            x, y = n, m
            for step in range(distance, -1, -1):
                previous = trace[step]
                diagonal = x - y
                if diagonal == -step or (
                    diagonal != step
                    and previous.get(diagonal - 1, -1) < previous.get(diagonal + 1, -1)
                ):
                    previous_diagonal = diagonal + 1
                else:
                    previous_diagonal = diagonal - 1
                px = previous.get(previous_diagonal, 0)
                py = px - previous_diagonal
                while x > px and y > py:
                    operations.append("equal")
                    x -= 1
                    y -= 1
                if step:
                    if x == px:
                        operations.append("insert")
                        y -= 1
                    else:
                        operations.append("delete")
                        x -= 1
            while x and y:
                operations.append("equal")
                x -= 1
                y -= 1
            operations.extend(["delete"] * x)
            operations.extend(["insert"] * y)
            operations.reverse()
            return _opcodes_from_operations(operations)
    return None


def _opcodes_from_operations(operations: list[str]):
    opcodes, old_index, new_index, change_start = [], 0, 0, None

    def flush_change():
        nonlocal change_start
        if change_start is None:
            return
        old_start, new_start = change_start
        tag = (
            "replace"
            if old_start < old_index and new_start < new_index
            else "delete"
            if old_start < old_index
            else "insert"
        )
        opcodes.append((tag, old_start, old_index, new_start, new_index))
        change_start = None

    for operation in operations:
        if operation == "equal":
            flush_change()
            if opcodes and opcodes[-1][0] == "equal":
                tag, a, _, c, _ = opcodes[-1]
                opcodes[-1] = (tag, a, old_index + 1, c, new_index + 1)
            else:
                opcodes.append(("equal", old_index, old_index + 1, new_index, new_index + 1))
            old_index += 1
            new_index += 1
        else:
            if change_start is None:
                change_start = (old_index, new_index)
            if operation == "delete":
                old_index += 1
            else:
                new_index += 1
    flush_change()
    return opcodes


def text_edits(before: str, after: str) -> list[TextEdit]:
    """Return non-overlapping edits in descending order, at zero-based UTF-16 offsets."""
    if before == after:
        return []
    if after.startswith(before) or before.startswith(after):
        return [_trim_edit(before, after, 0)]
    old_lines = before.splitlines(keepends=True)
    new_lines = after.splitlines(keepends=True)
    max_distance = min(256, _MAX_DIFF_WORK // max(1, len(old_lines) + len(new_lines)))
    opcodes = _myers_opcodes(old_lines, new_lines, max_distance)
    if opcodes is None:
        # Large unrelated rewrites exceed the edit budget; use one exact bounded patch.
        return [_trim_edit(before, after, 0)]
    offsets = list(accumulate(map(utf16_length, old_lines), initial=0))
    return [
        _trim_edit("".join(old_lines[a:b]), "".join(new_lines[c:d]), offsets[a])
        for tag, a, b, c, d in reversed(opcodes)
        if tag != "equal"
    ]

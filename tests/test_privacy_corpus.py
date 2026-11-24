import json
import random
import re
import string
from collections import Counter
from pathlib import Path

from ai_memory.privacy import find_sensitive, redact_sensitive

_SYNTHETIC = re.compile(r"\{\{synthetic\|([^|]+)\|([anA@_./:-]+)\}\}")
_ALPHABETS = {
    "a": string.ascii_lowercase,
    "A": string.ascii_uppercase,
    "n": string.digits,
    "@": "A",
}


def _materialize(value: str, row_number: int) -> str:
    """Create fresh test-only credentials from the stored format, with no real token material."""

    def replace(match: re.Match[str]) -> str:
        prefix, shape = match.groups()
        rng = random.Random(f"{row_number}:{prefix}:{shape}")
        return prefix + "".join(rng.choice(_ALPHABETS[c]) if c in _ALPHABETS else c for c in shape)

    return _SYNTHETIC.sub(replace, value)


def test_independent_masking_corpus():
    path = Path(__file__).with_name("fixtures") / "secrets-corpus.jsonl"
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for number, case in enumerate(cases, 1):
        case["text"] = _materialize(case["text"], number)
        case["secrets"] = [_materialize(secret, number) for secret in case["secrets"]]
    languages = Counter(case["language"] for case in cases)
    assert len({case["text"] for case in cases}) == len(cases) >= 200
    assert (languages["mn"] + languages["mixed"]) / len(cases) >= 0.30

    misses = []
    labels = safe = false_masked = 0
    for number, case in enumerate(cases, 1):
        text = case["text"]
        masked = redact_sensitive(text)
        assert len(masked) == len(text), number
        assert redact_sensitive(masked) == masked, number
        assert not find_sensitive(masked), number

        eligible = bytearray(b"\x01" * len(text))
        for secret in case["secrets"]:
            assert text.count(secret) == 1, number
            start = text.index(secret)
            eligible[start : start + len(secret)] = b"\x00" * len(secret)
            lines = secret.splitlines(keepends=True)
            if (
                len(lines) > 2
                and lines[0].startswith("-----BEGIN ")
                and lines[-1].startswith("-----END ")
            ):
                # Public PEM header and footer identify the key format. The payload is private.
                start += len(lines[0])
                secret = "".join(lines[1:-1])
            if any(ch not in "*\r\n" for ch in masked[start : start + len(secret)]):
                misses.append(number)
            labels += 1
        safe += sum(eligible)
        false_masked += sum(
            is_safe and old != new for is_safe, old, new in zip(eligible, text, masked, strict=True)
        )

    assert len(misses) / labels <= 0.05, misses
    assert false_masked / safe <= 0.001, false_masked

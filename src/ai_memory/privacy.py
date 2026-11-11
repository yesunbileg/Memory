"""Find and mask credential values before cloud upload.

Redaction is performed on upload snapshots only. Callers must keep the original
text in the local archive.
"""

import re

PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
    "api_key": re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b"
    ),
    "google_cookie": re.compile(r"\b(?:__Secure-[13]PSID|SID|HSID|SAPISID)=(?P<value>[^\s;,\"']+)"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"),
}


_ASSIGNMENT = re.compile(
    r"(?i)\b(?P<name>api[_-]?key|password|passwd|authorization|access[_-]?token|"
    r"refresh[_-]?token|cookie|session[_-]?token)\b[\"']?\s*[=:]\s*"
    r"(?:(?:bearer|basic|token)\s+)?"
)
_EXPRESSION = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\s*\(|\s*\[|\.[A-Za-z_])")
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN (?P<label>(?:[A-Z0-9]+ )?PRIVATE KEY)-----")
_TERMINAL_PUNCTUATION = ".,!?)]}"
_EXPRESSION_SUFFIX = re.compile(
    r"^\s*(?:===|!==|==|!=|<=|>=|&&|\|\||\?\?|\+|\-|\*|/|%|\?|"
    r"\b(?:and|or|if|else|is|in)\b|\.\s*[A-Za-z_]|\[|\()",
    re.IGNORECASE,
)
_COOKIE_PAIR = re.compile(r"(?P<name>[!#$%&'*+.^_`|~0-9A-Za-z-]+)=(?P<value>[^;\s,\"']+)")
_STRING_PREFIX = re.compile(r"(?i)(?P<prefix>[rubf]{1,2})(?P<quote>[\"'])")
_INTERPOLATION = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_][A-Za-z_0-9]*|\{\{.*?\}\}")


def _private_key_payload_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for begin in _PRIVATE_KEY_BEGIN.finditer(text):
        end_marker = f"-----END {begin.group('label')}-----"
        end_start = text.find(end_marker, begin.end())
        if end_start < 0:
            # No reliable value boundary: preserve unrelated text and let the
            # final detector block the malformed key instead of guessing.
            continue
        end = end_start
        if end > begin.end():
            spans.append((begin.end(), end))
    return spans


def _is_fully_masked(text: str, span: tuple[int, int]) -> bool:
    start, end = span
    return start < end and all(char == "*" for char in text[start:end])


def _quoted_value(text: str, start: int) -> tuple[int, int, int, str] | None:
    """Return string content bounds, end position, and delimiter at ``start``."""
    if start >= len(text):
        return None
    escaped_open = re.match(r"(?P<slashes>\\+)(?P<quote>[\"'])", text[start:])
    if escaped_open:
        quote = escaped_open.group("quote")
        content_start = start + escaped_open.end()
        for close in re.finditer(r"\\+(?P<quote>[\"'])", text[content_start:]):
            if close.group("quote") != quote:
                continue
            close_start = content_start + close.start()
            close_end = content_start + close.end()
            tail = text[close_end:]
            if not tail or tail[0] in "\"'\r\n,;)}#":
                return content_start, close_start, close_end, "\\" + quote
            if tail[0].isspace() and (len(tail) == 1 or tail.lstrip()[0] in ",;)}#"):
                return content_start, close_start, close_end, "\\" + quote
        return None

    quote = text[start]
    if quote not in "\"'`":
        return None
    delimiter = quote * 3 if quote in "\"'" and text.startswith(quote * 3, start) else quote
    content_start = start + len(delimiter)
    cursor = content_start
    while cursor < len(text):
        if text[cursor] == "\\":
            cursor += 2
            continue
        if text.startswith(delimiter, cursor):
            return content_start, cursor, cursor + len(delimiter), delimiter
        if quote != "`" and len(delimiter) == 1 and text[cursor] in "\r\n":
            return None
        cursor += 1
    return None


def _looks_like_expression(text: str, start: int, end: int) -> bool:
    if _EXPRESSION.match(text, start):
        return True
    # A following Markdown separator/list or another message is not part of
    # this assignment. Never let its operators hide a literal on this line.
    suffix = re.split(r"[\r\n]", text[end:], maxsplit=1)[0]
    suffix = suffix.split("#", 1)[0].split("//", 1)[0]
    return bool(_EXPRESSION_SUFFIX.search(suffix))


def _cookie_value_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    return [match.span("value") for match in _COOKIE_PAIR.finditer(text, start, end)]


def _assignment_value_spans(text: str, assignment: re.Match[str]) -> list[tuple[int, int]] | None:
    """Return credential value spans, or None when the RHS is an expression."""
    start = assignment.end()
    name = assignment.group("name").casefold().replace("_", "").replace("-", "")

    # Python and JavaScript string prefixes are syntax; f-strings and
    # interpolation are expressions and must stay intact.
    prefix = _STRING_PREFIX.match(text, start)
    if prefix:
        start += len(prefix.group("prefix"))

    quoted = _quoted_value(text, start)
    if quoted:
        value_start, value_end, after, delimiter = quoted
        if (
            prefix
            and "f" in prefix.group("prefix").casefold()
            and any(char in "{}" for char in text[value_start:value_end])
        ):
            return None
        if _INTERPOLATION.search(text, value_start, value_end):
            return None
        if _looks_like_expression(text, start, after):
            return None
        if name == "authorization":
            bearer = re.match(r"(?i)(?:Bearer|Basic|Token)\s+", text[value_start:value_end])
            if bearer:
                value_start += bearer.end()
        if name == "cookie":
            pairs = _cookie_value_spans(text, value_start, value_end)
            if pairs:
                return pairs
        return [(value_start, value_end)] if value_end > value_start else None

    if start < len(text) and text[start] in "$@{([":
        return None
    token_end = start
    while token_end < len(text) and text[token_end] not in " \t\r\n,;#\"'`":
        token_end += 1
    while token_end > start and text[token_end - 1] in _TERMINAL_PUNCTUATION:
        token_end -= 1
    if token_end <= start or _looks_like_expression(text, start, token_end):
        return None
    if name == "cookie":
        line_end = text.find("\n", start)
        if line_end < 0:
            line_end = len(text)
        pairs = _cookie_value_spans(text, start, line_end)
        if pairs:
            return pairs
    return [(start, token_end)]


def find_sensitive(text: str) -> list[str]:
    findings = []
    if any(
        text.find(f"-----END {begin.group('label')}-----", begin.end()) < 0
        for begin in _PRIVATE_KEY_BEGIN.finditer(text)
    ):
        findings.append("private_key")
    for label, pattern in PATTERNS.items():
        if label == "private_key":
            continue
        for match in pattern.finditer(text):
            span = match.span("value") if label == "google_cookie" else match.span()
            if not _is_fully_masked(text, span):
                findings.append(label)
                break

    for assignment in _ASSIGNMENT.finditer(text):
        spans = _assignment_value_spans(text, assignment)
        if spans and any(not _is_fully_masked(text, span) for span in spans):
            findings.append("credential_assignment")
            break

    for span in _private_key_payload_spans(text):
        payload = text[slice(*span)]
        if any(char not in "* \t\r\n" for char in payload):
            findings.append("private_key")
            break
    return findings


def _mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Mask merged spans while preserving every character's position."""
    if not spans:
        return text
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))

    result = list(text)
    for start, end in merged:
        result[start:end] = ["*" if char not in "\r\n" else char for char in text[start:end]]
    return "".join(result)


def redact_sensitive(text: str) -> str:
    """Replace recognized credential values with same-length asterisks.

    Labels, quotes, separators, line breaks, and all unrelated text remain
    unchanged. Assignment expressions such as ``password = get_password()``
    are left intact because they describe a credential source, not its value.
    """
    spans: list[tuple[int, int]] = []

    for label, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            if label == "private_key":
                # Keep the PEM marker and redact its payload below.
                continue
            if label == "google_cookie":
                spans.append(match.span("value"))
            else:
                spans.append(match.span())

    for assignment in _ASSIGNMENT.finditer(text):
        assignment_spans = _assignment_value_spans(text, assignment)
        if assignment_spans:
            spans.extend(assignment_spans)

    spans.extend(_private_key_payload_spans(text))

    return _mask_spans(text, spans)

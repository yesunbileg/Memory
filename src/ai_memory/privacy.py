"""Find and mask credential values before cloud upload.

Redaction is performed on upload snapshots only. Callers must keep the original
text in the local archive.
"""

import hashlib
import re
from collections import Counter
from math import log2

# Increment for any change to the outbound field allowlist or masking semantics.
MASKING_POLICY = 3


def snapshot_sha256(text: str | bytes) -> str:
    return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()


# Explicit provider token formats take precedence over the entropy fallback.
_PROVIDER_TOKEN = (
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk_(?:live|test)_[A-Za-z0-9]{10,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|rk_(?:live|test)_[A-Za-z0-9]{10,}"
    r"|whsec_[A-Za-z0-9]{10,}"
    r"|xox[abcdprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[0-9A-Za-z_-]{10,}"
    r"|ya29\.[0-9A-Za-z_-]{10,}"
    r"|SG\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{16,}"
    r"|hvs\.[A-Za-z0-9_-]{20,}"
    r"|[0-9]{8,12}:AA[A-Za-z0-9_-]{24,}"
    r"|github_pat_[A-Za-z0-9_]{10,}"
    r"|glpat-[A-Za-z0-9_-]{10,}"
    r"|npm_[A-Za-z0-9]{10,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|AKIA[A-Z0-9]{16}"
    r")(?![A-Za-z0-9_-])"
)

PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
    "api_key": re.compile(_PROVIDER_TOKEN),
    "google_cookie": re.compile(r"\b(?:__Secure-[13]PSID|SID|HSID|SAPISID)=(?P<value>[^\s;,\"']+)"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"),
    "url_password": re.compile(
        r"(?i)\b(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<userinfo>[^\s/@:]*):"
        r"(?P<value>[^\s/@]+)@"
    ),
    "database_password_flag": re.compile(
        r"(?m)^[ \t]*(?:\$[ \t]+)?(?:mysql|mysqldump|mariadb)[ \t]+"
        r"[^\r\n;|]*?[ \t]-p(?P<value>[^\s$'\"`;&|][^\s;&|]*)"
    ),
    "webhook": re.compile(
        r"https://(?:hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9_-]+"
        r"|discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+)"
    ),
    "credential_query": re.compile(
        r"(?i)[?&](?:token|access_token|auth_token|api_key|sig)=(?P<value>[^\s&<>\"'`]+)"
    ),
    "command_password": re.compile(
        r"(?im)^[ \t]*(?:curl[ \t]+[^\r\n]*?(?:-u|--user)[ \t]+[^:\s]+:"
        r"|sqlplus[ \t]+[^/\s]+/)(?P<value>[^\s$@\"'`]+)"
    ),
    "pgpass": re.compile(r"(?m)^[\w.-]+:\d{2,5}:[\w.-]+:[\w.-]+:(?P<value>[^\s]+)$"),
    "sql_password": re.compile(
        r"(?im)^\s*(?:(?:create|alter)[ \t]+(?:user|role)[^\r\n]+?[ \t]+(?:with[ \t]+)?password"
        r"|[^\r\n]*--[ \t]+(?:temp|temporary)[ \t]+password)[ \t]+['\"]?"
        r"(?P<value>[^\s;'\"]+)"
    ),
    "credential_default": re.compile(
        r"(?im)(?:variable[ \t]+[\"'][\w.-]*(?:password|secret|api[_-]?key)[\"'][ \t]*\{[^}]*?"
        r"|^(?P<indent>[ \t]*)[\w.-]*(?:password|secret|api[_-]?key):[ \t]*\n"
        r"(?:(?P=indent)[ \t]+[^\r\n]*\n)*?(?=(?P=indent)[ \t]+))"
        r"[ \t]*(?:default|example)[ \t]*[:=][ \t]*[\"']?(?P<value>[^\s,;'\"]+)"
    ),
}

_TOKEN_HINTS = {
    "api_key": (
        "sk_",
        "sk-",
        "rk_",
        "whsec_",
        "xox",
        "AIza",
        "ya29.",
        "SG.",
        "hvs.",
        ":AA",
        "github_pat_",
        "glpat-",
        "npm_",
        "ghp_",
        "gho_",
        "ghu_",
        "ghs_",
        "ghr_",
        "AKIA",
    ),
    "google_cookie": ("SID=",),
    "jwt": ("eyJ",),
    "url_password": ("://",),
    "database_password_flag": ("mysql", "mariadb"),
    "webhook": ("hooks.slack.com/", "/api/webhooks/"),
    "credential_query": ("?", "&"),
    "command_password": ("curl ", "sqlplus ", "curl\t", "sqlplus\t"),
    "pgpass": (":",),
}


_CREDENTIAL_SUFFIXES = (
    "secretaccesskey",
    "encryptionkey",
    "integrationkey",
    "clientsecret",
    "privatekey",
    "passphrase",
    "refreshtoken",
    "sessiontoken",
    "authorization",
    "password",
    "passwd",
    "secretkey",
    "apikey",
    "authtoken",
    "bottoken",
    "vaulttoken",
    "accesstoken",
    "secret",
    "cookie",
)
_ASSIGNMENT_CANDIDATE = re.compile(
    r"(?:password|passwd|passphrase|secret|authorization|cookie|token|key)[\"' \t]*[=:]"
)
_PROSE_CANDIDATES = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "api key",
    "api_key",
    "api-key",
    "apikey",
    "private key",
    "private_key",
    "private-key",
    "privatekey",
    "token",
)
_PROSE_OPERATORS = (":", " is", " was", " were", " are", "\tis", "\twas", "\twere", "\tare")

_ASSIGNMENT = re.compile(
    r"(?i)(?<![\w.-])(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]{0,79})[\"']?[ \t]*"
    r"(?P<sep>:=|=>|=(?!=)|:(?!=))[ \t]*"
    r"(?:(?P<scheme>bearer|basic|token)[ \t]+)?"
)
_PROSE_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?P<name>"
    r"api[ _-]?key|private[ _-]?key|secret[ _-]?key|client[ _-]?secret"
    r"|(?:auth|access|refresh|session|vault|bot)[ _-]?token|password|passwd|passphrase|secret)"
    r"(?:[ \t]*(?P<sep>:)[ \t]*|[ \t]+(?:is|was|were|are)[ \t]*:?[ \t]*"
    r"(?:(?:set|changed)[ \t]+to[ \t]+)?(?=(?:[rubf]{0,2})?[\"'`\\]))"
)
_MONGOLIAN_NAME = (
    r"(?:нууц[ \t]*үг(?:ээ|ийг|ийн)?"
    r"|нэвтрэх[ \t]*үг"
    r"|нууц[ \t]*түлхүүр"
    r"|api[ \t]*түлхүүр"
    r"|түлхүүр"
    r"|токен"
    r"|нууц(?:[ \t]+код)?)"
)
_SUBSCRIPT_ASSIGNMENT = re.compile(
    r"(?i)\[\s*[\"'](?P<name>[A-Za-z0-9][A-Za-z0-9_.-]{0,79})[\"']\s*\]\s*"
    r"(?P<sep>:=|=(?!=))\s*"
)
_MONGOLIAN_ASSIGNMENT = re.compile(
    rf"(?i)(?<!\w)(?P<name>{_MONGOLIAN_NAME})[ \t]*(?P<sep>:=|=>|=(?!=)|:(?!=))[ \t]*"
)
_MONGOLIAN_PROSE = re.compile(
    rf"(?i)(?<![\w.-])(?P<name>{_MONGOLIAN_NAME})[ \t]+(?:нь|бол)[ \t]*:?[ \t]*"
    r"(?=(?:[rubf]{0,2})?[\"'`\\])"
)
_EXPRESSION = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\s*\(|\s*\[|\.[A-Za-z_])")
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN (?P<label>(?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----")
_TERMINAL_PUNCTUATION = ".,!?)]}"
_EXPRESSION_SUFFIX = re.compile(
    r"[ \t]*(?:===|!==|==|!=|<=|>=|&&|\|\||\?\?|\+|-|\*|/|%|\?(?=[ \t]*\S)|"
    r"\b(?:and|or|if|else|is|in)\b|\.[A-Za-z_]|\[|\()",
    re.IGNORECASE,
)
_COOKIE_PAIR = re.compile(r"(?P<name>[!#$%&'*+.^_`|~0-9A-Za-z-]+)=(?P<value>[^;\s,\"']+)")
_STRING_PREFIX = re.compile(r"(?i)(?P<prefix>[rubf]{1,2})(?P<quote>[\"'])")
_ESCAPED_QUOTE_OPEN = re.compile(r"(?P<slashes>\\+)(?P<quote>[\"'])")
_ESCAPED_QUOTE = re.compile(r"(?P<slashes>\\+)(?P<quote>[\"'])")
_INTERPOLATION = re.compile(r"\$\{[^}]*\}|\$\([^()]*\)|\$[A-Za-z_][A-Za-z_0-9]*|\{\{.*?\}\}")
_DOTTED_PASSPHRASE = re.compile(
    r"(?:[A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*){2,}"
    r"|[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){3,})"
)
_BARE_VALUE = re.compile(r"(?!#)[^ \t\r\n,;&\"'`]+")
_PLAIN_LINE_VALUE = re.compile(r"[A-Za-z0-9_*-]+(?=[ \t]*(?:\r?\n|\Z))")
_AUTH_VALUE = re.compile(r"[A-Za-z0-9_~+/.=*-]+")
_PLACEHOLDER = re.compile(r"(?i)your(?:[_-][A-Za-z0-9]+)*[_-]here|<[^<>\r\n]+>")
_PUBLIC_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PUBLIC KEY-----[\s\S]*?-----END (?:[A-Z0-9]+ )*PUBLIC KEY-----"
)
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{28,}")
_HEX_TOKEN = re.compile(r"[a-fA-F0-9-]+")
_TOKEN_DELIMITERS = " \t\r\n\"'`<>|(){}[]"
_DIGIT = re.compile(r"[0-9]")
_LETTER = re.compile(r"[A-Za-z]")
_NON_SECRET_LITERAL = re.compile(r"(?i)(?:none|null|undefined|nil|true|false)")
_TYPE_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_TYPE_NAMES = {
    "any",
    "annotated",
    "bool",
    "boolean",
    "bytearray",
    "bytes",
    "decimal",
    "dict",
    "final",
    "float",
    "int",
    "integer",
    "list",
    "never",
    "nil",
    "none",
    "number",
    "object",
    "optional",
    "secretbytes",
    "secretstr",
    "set",
    "str",
    "string",
    "text",
    "tuple",
    "union",
    "unknown",
    "void",
}
_WHITESPACE = re.compile(r"\s*")
_LINE_END = re.compile(r"[\r\n]")
_MASKED_CHARACTER = re.compile(r"[^\r\n]")
_BRACES = re.compile(r"[{}]")
_CODE_CONTROL_START = re.compile(
    r"[ \t]*(?:if|elif|while|for|with|except|def|class|match|case|else|try|finally)\b"
)
_CODE_COMPARISON = re.compile(r"(?:==|!=|<=|>=|\b(?:in|is|and|or)\b)")


class _EmptyUnterminatedQuote:
    """Marker for an opening quote with no value characters left to mask."""


_EMPTY_UNTERMINATED = _EmptyUnterminatedQuote()


def _assignment_name(assignment: re.Match[str], pattern: re.Pattern[str]) -> str:
    name = assignment.group("name").casefold()
    if pattern in (_MONGOLIAN_ASSIGNMENT, _MONGOLIAN_PROSE):
        return name
    name = name.replace("_", "").replace(".", "").replace("-", "").replace(" ", "")
    return name if name.endswith(_CREDENTIAL_SUFFIXES) else ""


def _private_key_payload_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for begin in _PRIVATE_KEY_BEGIN.finditer(text):
        end_marker = f"-----END {begin.group('label')}-----"
        end_start = text.find(end_marker, begin.end())
        if end_start < 0:
            # A later document-level check must not pair this malformed
            # opening marker with a different message's closing marker.
            spans.append((begin.start(), len(text)))
            break
        if end_start > begin.end():
            spans.append((begin.end(), end_start))
    return spans


def _is_fully_masked(text: str, span: tuple[int, int]) -> bool:
    start, end = span
    return start < end and all(char in "*\r\n" for char in text[start:end])


def _line_end(text: str, start: int) -> int:
    match = _LINE_END.search(text, start)
    return match.start() if match else len(text)


def _quoted_value(text: str, start: int) -> tuple[int, int, int, str] | None:
    """Return string content bounds, end position, and delimiter at ``start``."""
    if start >= len(text):
        return None
    escaped_open = _ESCAPED_QUOTE_OPEN.match(text, start)
    if escaped_open:
        quote = escaped_open.group("quote")
        content_start = escaped_open.end()
        for close in _ESCAPED_QUOTE.finditer(text, content_start, _line_end(text, content_start)):
            if close.group("quote") != quote:
                continue
            close_start = close.start()
            close_end = close.end()
            if close_end >= len(text) or text[close_end] in "\"'\r\n,;)}#":
                return content_start, close_start, close_end, "\\" + quote
            if text[close_end].isspace():
                tail = _WHITESPACE.match(text, close_end)
                next_start = tail.end() if tail else close_end
                if next_start >= len(text) or text[next_start] in ",;)}#":
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


def _unterminated_quote_span(
    text: str, start: int, prefix: re.Match[str] | None
) -> tuple[int, int] | _EmptyUnterminatedQuote | None:
    """Return a bounded fallback span for a literal with no closing quote.

    A single-line quote consumes the rest of its line; a triple quote or an
    unclosed backtick consumes the rest of the input. Interpolated values stay
    intact because they describe a source rather than a literal secret.
    """
    escaped = _ESCAPED_QUOTE_OPEN.match(text, start)
    if escaped:
        quote = escaped.group("quote")
        content_start = escaped.end()
        multiline = False
    elif start < len(text) and text[start] in "\"'`":
        quote = text[start]
        delimiter = quote * 3 if quote in "\"'" and text.startswith(quote * 3, start) else quote
        content_start = start + len(delimiter)
        multiline = len(delimiter) == 3 or quote == "`"
    else:
        return None

    content_end = len(text)
    if not multiline:
        content_end = _line_end(text, content_start)
        # A quote at the end of an otherwise unclosed literal is usually an
        # outer serialization delimiter (for example YAML around a clipped
        # literal), not part of the credential value.
        if content_end > content_start and text[content_end - 1] in "\"'":
            content_end -= 1

    if content_end <= content_start:
        if multiline:
            return (prefix.start() if prefix else start), content_end
        return _EMPTY_UNTERMINATED
    if _INTERPOLATION.search(text, content_start, content_end) or (
        prefix
        and "f" in prefix.group("prefix").casefold()
        and _BRACES.search(text, content_start, content_end)
    ):
        return None
    if multiline:
        return (prefix.start() if prefix else start), content_end
    return content_start, content_end


def _looks_like_expression(text: str, start: int, end: int) -> bool:
    if _EXPRESSION.match(text, start):
        return True
    # A following Markdown separator/list or another message is not part of
    # this assignment. Never let its operators hide a literal on this line.
    # Only the immediate suffix matters, so cap the scan for very long lines.
    line_end = min(len(text), end + 256)
    for newline in ("\r", "\n"):
        found = text.find(newline, end, line_end)
        if 0 <= found < line_end:
            line_end = found
    cursor = end
    while cursor < line_end and text[cursor] in " \t":
        cursor += 1
    if cursor < line_end and (text[cursor] == "#" or text.startswith("//", cursor)):
        return False
    return bool(_EXPRESSION_SUFFIX.match(text, end, line_end))


def _annotation_value_start(text: str, start: int) -> int | None | _EmptyUnterminatedQuote:
    """Skip a type annotation after a colon, returning where the value starts."""
    token = _TYPE_TOKEN.match(text, start)
    if not token:
        return None
    token_end = token.end()
    line_end = min(len(text), start + 256)
    for newline in ("\r", "\n"):
        found = text.find(newline, start, line_end)
        if 0 <= found < line_end:
            line_end = found
    cursor = token_end
    while cursor < line_end and text[cursor] in " \t":
        cursor += 1
    if cursor < line_end and text[cursor] == "=" and not text.startswith("==", cursor):
        if text.startswith("=>", cursor):
            return None
        cursor += 1
        while cursor < line_end and text[cursor] in " \t":
            cursor += 1
        return cursor
    if token.group().casefold() in _TYPE_NAMES:
        return _EMPTY_UNTERMINATED
    return None


def _cookie_value_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    spans = []
    for match in _COOKIE_PAIR.finditer(text, start, end):
        name, value = match.group("name", "value")
        if name == "theme" and value in {"dark", "light", "system"}:
            continue
        if name == "lang" and re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", value):
            continue
        spans.append(match.span("value"))
    return spans


def _expression_literal_spans(text: str, start: int) -> list[tuple[int, int]]:
    """Find top-level literal operands, not function arguments or lookup keys."""
    spans = []
    depth = 0
    cursor = start
    while cursor < len(text):
        char = text[cursor]
        if char in "\r\n;#" or text.startswith("//", cursor):
            break
        if char == "," and depth == 0:
            break
        prefix = _STRING_PREFIX.match(text, cursor)
        quoted_start = cursor + len(prefix.group("prefix")) if prefix else cursor
        quoted = _quoted_value(text, quoted_start) if prefix or char in "\"'`\\" else None
        if quoted:
            value_start, value_end, after, _ = quoted
            interpolated = _INTERPOLATION.search(text, value_start, value_end) or (
                prefix
                and "f" in prefix.group("prefix").casefold()
                and _BRACES.search(text, value_start, value_end)
            )
            if depth == 0 and value_end > value_start and not interpolated:
                spans.append((value_start, value_end))
            cursor = after
            continue
        if prefix or char in "\"'`\\":
            fallback = _unterminated_quote_span(text, quoted_start, prefix)
            if isinstance(fallback, tuple):
                spans.append(fallback)
            break
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                break
            depth -= 1
        cursor += 1
    return spans


def _assignment_value_spans(
    text: str, assignment: re.Match[str], name: str
) -> list[tuple[int, int]] | None:
    """Return literal spans when the assignment has a credential value."""
    start = assignment.end()
    separator = assignment.group("sep") if "sep" in assignment.re.groupindex else ""
    if separator == "=" and (plain := _PLAIN_LINE_VALUE.match(text, start)):
        return None if _NON_SECRET_LITERAL.fullmatch(text, start, plain.end()) else [plain.span()]
    if (
        name.endswith("authorization")
        and assignment.re is _ASSIGNMENT
        and assignment.group("scheme")
    ):
        auth = _AUTH_VALUE.match(text, start)
        return [(start, start + len(auth.group().rstrip(".")))] if auth else None

    if separator == ":" and name != "cookie":
        annotation_start = _annotation_value_start(text, start)
        if annotation_start is _EMPTY_UNTERMINATED:
            return None
        if annotation_start is not None:
            start = annotation_start
        else:
            # A colon that terminates an ``if``/``for``/``def`` condition or a
            # comparison is syntax, not a credential assignment. A literal
            # mapping before the candidate means the colon belongs to that
            # mapping, even when the line also starts with a control keyword.
            line_start = assignment.start()
            while line_start and text[line_start - 1] not in "\r\n":
                line_start -= 1
            has_mapping = text.find("{", line_start, assignment.start()) >= 0
            if not has_mapping and (
                _CODE_CONTROL_START.match(text, line_start, assignment.start())
                or (
                    assignment.re is _ASSIGNMENT
                    and _CODE_COMPARISON.search(text, line_start, assignment.start())
                )
            ):
                return None

    # Python and JavaScript string prefixes are syntax; f-strings and
    # interpolation are expressions and must stay intact.
    prefix = _STRING_PREFIX.match(text, start)
    if prefix:
        start += len(prefix.group("prefix"))

    quoted = _quoted_value(text, start)
    if quoted:
        value_start, value_end, after, delimiter = quoted
        expression_spans = (
            _expression_literal_spans(text, after)
            if _looks_like_expression(text, start, after)
            else []
        )
        if (
            prefix
            and "f" in prefix.group("prefix").casefold()
            and _BRACES.search(text, value_start, value_end)
        ):
            return expression_spans or None
        interpolation = (
            _INTERPOLATION.fullmatch(text, value_start, value_end)
            if delimiter == "'"
            else _INTERPOLATION.search(text, value_start, value_end)
        )
        if interpolation:
            return expression_spans or None
        if name == "authorization":
            bearer = re.match(r"(?i)(?:Bearer|Basic|Token)\s+", text[value_start:value_end])
            if bearer:
                value_start += bearer.end()
        if name == "cookie":
            pairs = _cookie_value_spans(text, value_start, value_end)
            if pairs:
                return pairs
        return ([(value_start, value_end)] + expression_spans) if value_end > value_start else None

    if start < len(text) and (text[start] in "\"'`" or _ESCAPED_QUOTE_OPEN.match(text, start)):
        # An opening delimiter without a closing delimiter is still an actual
        # literal when it has value characters. Dynamic templates stay intact.
        fallback = _unterminated_quote_span(text, start, prefix)
        return [fallback] if isinstance(fallback, tuple) else None

    if start < len(text) and text[start] in "$@{([<":
        return None
    value = _BARE_VALUE.match(text, start)
    if not value:
        return None
    token_end = value.end()
    while token_end > start and text[token_end - 1] in _TERMINAL_PUNCTUATION:
        token_end -= 1
    if token_end <= start:
        return None
    token = text[start:token_end]
    # Three title-case or four lowercase dotted words can be a passphrase. They
    # are valid values now, not an unclosed source expression.
    dotted = "." in token
    if dotted and _DOTTED_PASSPHRASE.fullmatch(token):
        return [(start, token_end)]
    if _NON_SECRET_LITERAL.fullmatch(token):
        return None
    if name == "cookie":
        pairs = _cookie_value_spans(text, start, _line_end(text, start))
        if pairs:
            return pairs
    if (
        dotted
        or "(" in token
        or "[" in token
        or (token_end < len(text) and text[token_end] not in "\r\n")
    ) and _looks_like_expression(text, start, token_end):
        return _expression_literal_spans(text, start) or None
    return [(start, token_end)]


def _token_patterns(text: str, folded: str):
    for label, hints in _TOKEN_HINTS.items():
        if any(hint in text for hint in hints):
            yield label, PATTERNS[label]
    if "password" in folded and any(word in folded for word in ("--", "create", "alter")):
        yield "sql_password", PATTERNS["sql_password"]
    if ("default" in folded or "example" in folded) and any(
        word in folded for word in ("password", "secret", "api")
    ):
        yield "credential_default", PATTERNS["credential_default"]


def _token_span(match: re.Match[str]) -> tuple[int, int]:
    return match.span("value") if "value" in match.re.groupindex else match.span()


def _assignment_patterns(text: str, folded: str):
    if _ASSIGNMENT_CANDIDATE.search(folded):
        yield _ASSIGNMENT
    if "[" in text and "=" in text:
        yield _SUBSCRIPT_ASSIGNMENT
    if any(key in folded for key in _PROSE_CANDIDATES) and any(
        op in folded for op in _PROSE_OPERATORS
    ):
        yield _PROSE_ASSIGNMENT
    if any(name in folded for name in ("нууц", "нэвтрэх", "түлхүүр", "токен")):
        yield _MONGOLIAN_ASSIGNMENT
        yield _MONGOLIAN_PROSE


def _entropy_spans(text: str):
    public = iter(_PUBLIC_KEY.finditer(text))
    public_key = next(public, None)
    item_end, skip_item = 0, False
    for match in _LONG_TOKEN.finditer(text):
        value = match.group()
        if _HEX_TOKEN.fullmatch(value) or not (_LETTER.search(value) and _DIGIT.search(value)):
            continue
        start, end = match.span()
        while public_key and public_key.end() <= start:
            public_key = next(public, None)
        if public_key and public_key.start() < start < public_key.end():
            continue
        if start >= item_end:
            left, item_end = start, end
            while left and text[left - 1] not in _TOKEN_DELIMITERS:
                left -= 1
            while item_end < len(text) and text[item_end] not in _TOKEN_DELIMITERS:
                item_end += 1
            skip_item = "/" in text[left:item_end] or "\\" in text[left:item_end]
        if skip_item:
            continue
        size = len(value)
        entropy = log2(size) - sum(n * log2(n) for n in Counter(value).values()) / size
        if entropy >= 4.0:
            yield start, end


def find_sensitive(text: str) -> list[str]:
    findings = []
    folded = text.casefold()
    for label, pattern in _token_patterns(text, folded):
        for match in pattern.finditer(text):
            span = _token_span(match)
            if _requires_mask(text, span):
                findings.append(label)
                break

    for pattern in _assignment_patterns(text, folded):
        found = False
        for assignment in pattern.finditer(text):
            name = _assignment_name(assignment, pattern)
            spans = _assignment_value_spans(text, assignment, name) if name else None
            if spans and any(_requires_mask(text, span) for span in spans):
                found = True
                break
        if found:
            findings.append("credential_assignment")
            break

    if any(not _is_fully_masked(text, span) for span in _private_key_payload_spans(text)):
        findings.append("private_key")
    if next(_entropy_spans(text), None) is not None:
        findings.append("credential_entropy")
    return findings


def _requires_mask(text: str, span: tuple[int, int]) -> bool:
    start, end = span
    if start >= end:
        return False
    first = text[start]
    if first in "*\r\n" and _is_fully_masked(text, span):
        return False
    if first in "yY<" and _PLACEHOLDER.fullmatch(text, start, end):
        return False
    if end - start in {4, 8} and text[start:end].casefold() in {"password", "changeme", "test"}:
        context = text[max(0, start - 160) : min(len(text), end + 100)].casefold()
        if any(
            marker in context
            for marker in (
                "template",
                "placeholder",
                ".env.example",
                "fixture",
                "your_api_key_here",
            )
        ):
            return False
    return True


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

    parts = []
    cursor = 0
    for start, end in merged:
        parts.append(text[cursor:start])
        value = text[start:end]
        parts.append(
            _MASKED_CHARACTER.sub("*", value) if _LINE_END.search(value) else "*" * len(value)
        )
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def redact_sensitive(text: str) -> str:
    """Replace recognized credential values with same-length asterisks.

    Labels, quotes, separators, line breaks, and all unrelated text remain
    unchanged. Assignment expressions such as ``password = get_password()``
    are left intact because they describe a credential source, not its value.
    Identifiable literal operands are masked even inside expressions. An
    actual unterminated literal or dotted passphrase is masked using a bounded
    fallback so the upload gate is not left with an unmaskable value. This is a
    bounded detector, not comprehensive secret detection.
    """
    spans: list[tuple[int, int]] = []
    folded = text.casefold()

    for _, pattern in _token_patterns(text, folded):
        spans.extend(
            span
            for match in pattern.finditer(text)
            if _requires_mask(text, span := _token_span(match))
        )

    for pattern in _assignment_patterns(text, folded):
        for assignment in pattern.finditer(text):
            name = _assignment_name(assignment, pattern)
            assignment_spans = _assignment_value_spans(text, assignment, name) if name else None
            if assignment_spans:
                spans.extend(span for span in assignment_spans if _requires_mask(text, span))

    spans.extend(_private_key_payload_spans(text))
    spans.extend(_entropy_spans(text))

    return _mask_spans(text, spans)

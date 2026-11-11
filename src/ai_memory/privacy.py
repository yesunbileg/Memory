"""Conservative upload gate. Never redact or rewrite the local source archive."""

import re

PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "api_key": re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b"
    ),
    "google_cookie": re.compile(r"\b(?:__Secure-[13]PSID|SID|HSID|SAPISID)=[^\s;]{8,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"),
}


_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|password|passwd|authorization|access[_-]?token|"
    r"refresh[_-]?token|cookie|session[_-]?token)\b[\"']?\s*[=:]\s*"
    r"(?:bearer\s+)?"
)
_VALUE = re.compile(r"""(?:\\+["'][^"'\n]{8,}\\+["']|["'][^"'\n]{8,}["']|[^\s"'`,;]{8,})""")
_EXPRESSION = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\s*\(|\s*\[|\.[A-Za-z_])")


def find_sensitive(text: str) -> list[str]:
    findings = [label for label, pattern in PATTERNS.items() if pattern.search(text)]
    for assignment in _ASSIGNMENT.finditer(text):
        start = assignment.end()
        # Calls, member access and indexing describe where a credential comes from.
        # Quoted values and bare literal tokens still go through the upload gate.
        if not _EXPRESSION.match(text, start) and _VALUE.match(text, start):
            findings.append("credential_assignment")
            break
    return findings

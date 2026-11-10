"""Conservative upload gate. Never redact or rewrite the local source archive."""

import re

PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "api_key": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b"),
    "credential_assignment": re.compile(r'''(?i)\b(?:api[_-]?key|password|passwd|authorization|access[_-]?token|refresh[_-]?token|cookie|session[_-]?token)\b\s*[=:]\s*["']?(?:bearer\s+)?[^\s"'`,;]{8,}'''),
    "google_cookie": re.compile(r"\b(?:__Secure-[13]PSID|SID|HSID|SAPISID)=[^\s;]{8,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"),
}


def find_sensitive(text: str) -> list[str]:
    return [label for label, pattern in PATTERNS.items() if pattern.search(text)]

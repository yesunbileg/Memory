"""Allowlisted diagnostics; exception messages may contain private source content."""


class PublicInputError(ValueError):
    """Actionable application validation with no private data in its message."""


_PROVIDER_MESSAGES = {
    "auth_required": "NotebookLM authentication is missing or expired. Check your configured browser login.",
    "rate_limited": "NotebookLM rate limit reached. Retry later.",
    "processing_failed": "NotebookLM source processing failed.",
    "not_ready": "NotebookLM source is not ready. Retry sync later.",
    "timeout": "NotebookLM request timed out. Retry later.",
    "network_error": "NotebookLM connection failed. Check connectivity and retry.",
    "upload_registration_unavailable": (
        "NotebookLM uploads require an SDK that can persist the source ID before sending data."
    ),
    "upload_registration_failed": "NotebookLM registration could not be safely recorded.",
    "notebooklm_error": "NotebookLM request failed. Check login and retry.",
}


def provider_failure(error):
    """Never reflect a provider-supplied message or unknown code."""
    code = getattr(error, "code", None)
    if not isinstance(code, str) or code not in _PROVIDER_MESSAGES:
        code = "notebooklm_error"
    return {"code": code, "message": _PROVIDER_MESSAGES[code]}


SAFE_FAILURES = frozenset(
    {
        *_PROVIDER_MESSAGES,
        "failed_source_cleanup",
        "SensitiveData",
        "SnapshotError",
        "UploadUncertain",
        "remote_missing",
        "SourceQuota",
        "ConnectionError",
        "TimeoutError",
        "CapacityError",
        "docs_web_unavailable",
        "docs_web_brave_required",
        "docs_web_auth_required",
        "docs_web_uncertain",
        "docs_web_not_sent",
        "docs_web_conflict",
        "docs_web_create_uncertain",
        "docs_web_title_unconfirmed",
        "docs_web_link_uncertain",
    }
)


def failure_code(error: Exception) -> str:
    code = getattr(error, "code", type(error).__name__)
    return code if isinstance(code, str) and code in SAFE_FAILURES else "operation_failed"

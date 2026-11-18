"""Allowlisted diagnostics; exception messages may contain private source content."""


class PublicInputError(ValueError):
    """Actionable application validation with no private data in its message."""


def provider_failure(error):
    """Never reflect a provider-supplied message or unknown code."""
    messages = {
        "auth_required": "NotebookLM authentication is missing or expired. Check your configured browser login.",
        "rate_limited": "NotebookLM rate limit reached. Retry later.",
        "processing_failed": "NotebookLM source processing failed.",
        "not_ready": "NotebookLM source is not ready. Retry sync later.",
        "timeout": "NotebookLM request timed out. Retry later.",
        "network_error": "NotebookLM connection failed. Check connectivity and retry.",
        "notebooklm_error": "NotebookLM request failed. Check login and retry.",
    }
    code = getattr(error, "code", None)
    if not isinstance(code, str) or code not in messages:
        code = "notebooklm_error"
    return {"code": code, "message": messages[code]}


SAFE_FAILURES = frozenset(
    {
        "auth_required",
        "rate_limited",
        "processing_failed",
        "not_ready",
        "timeout",
        "network_error",
        "notebooklm_error",
        "failed_source_cleanup",
        "SensitiveData",
        "SnapshotError",
        "UploadUncertain",
        "remote_missing",
        "SourceQuota",
        "ConnectionError",
        "TimeoutError",
        "CapacityError",
    }
)


def failure_code(error: Exception) -> str:
    code = getattr(error, "code", type(error).__name__)
    return code if isinstance(code, str) and code in SAFE_FAILURES else "operation_failed"

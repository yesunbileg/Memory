"""Allowlisted diagnostics; exception messages may contain private source content."""

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
        "SourceQuota",
        "ConnectionError",
        "TimeoutError",
        "CapacityError",
    }
)


def failure_code(error: Exception) -> str:
    code = getattr(error, "code", type(error).__name__)
    return code if isinstance(code, str) and code in SAFE_FAILURES else "operation_failed"

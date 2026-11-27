"""Opt-in, content-free timing of the MCP retrieval path."""

import json
import os
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

_request = ContextVar("memory_trace_request", default=None)


def trace(stage, **metadata):
    request = _request.get()
    if request is not None:
        print(
            json.dumps(
                {
                    "memory_trace": request,
                    "stage": stage,
                    "monotonic_ns": time.monotonic_ns(),
                    **metadata,
                }
            ),
            file=sys.stderr,
            flush=True,
        )


@contextmanager
def query_trace():
    if os.environ.get("AI_MEMORY_TRACE") != "1":
        yield
        return
    token = _request.set(uuid4().hex)
    try:
        trace("mcp_received")
        yield
    finally:
        _request.reset(token)


@contextmanager
def sdk_chat_trace(chat):
    """Observe the pinned SDK's stream boundary without changing its behavior."""
    if _request.get() is None or not hasattr(chat, "_stream_answer"):
        yield
        return
    import httpx

    original = chat._stream_answer
    original_bytes = httpx.Response.aiter_bytes

    async def timed_bytes(response, *args, **kwargs):
        try:
            is_chat = "GenerateFreeFormStreamed" in response.request.url.path
        except RuntimeError:
            is_chat = False
        first = True
        async for chunk in original_bytes(response, *args, **kwargs):
            if is_chat and first and chunk:
                first = False
                trace("notebook_first_response_bytes")
            yield chunk
        if is_chat:
            trace("notebook_response_body_ready")

    async def stream(*args, **kwargs):
        trace("notebook_request_start")
        result = await original(*args, **kwargs)
        trace("notebook_answer_and_citations_ready")
        return result

    chat._stream_answer = stream
    httpx.Response.aiter_bytes = timed_bytes
    try:
        yield
    finally:
        httpx.Response.aiter_bytes = original_bytes
        del chat._stream_answer

"""The audit meter counts observed payloads, not estimated TLS wire traffic."""

import httpx

from scripts.audit_docs import Evidence


class SyntheticStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"hello"
        yield b"world"


async def test_notebook_meter_tracks_complete_partial_and_unread_responses(tmp_path):
    meter = Evidence(tmp_path)

    async def handle(_request):
        return httpx.Response(200, stream=SyntheticStream())

    with meter.measure_notebook():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            meter.phase = "complete"
            async with client.stream("POST", "https://example.invalid", content=b"abc") as reply:
                assert await reply.aread() == b"helloworld"
            meter.phase = "partial"
            async with client.stream("GET", "https://example.invalid") as reply:
                chunks = reply.aiter_raw()
                assert await anext(chunks) == b"hello"
                await chunks.aclose()
            meter.phase = "unread"
            async with client.stream("GET", "https://example.invalid"):
                pass
    assert meter.network["complete"]["known_request_body_bytes"] == 3
    assert meter.network["complete"]["known_response_body_bytes"] == 10
    assert meter.network["complete"]["streamed_responses_completed"] == 1
    assert meter.network["partial"]["known_response_body_bytes"] == 5
    assert meter.network["partial"]["streamed_responses_incomplete"] == 1
    assert meter.network["unread"]["known_response_body_bytes"] == 0
    assert meter.network["unread"]["streamed_responses_unmeasured"] == 1

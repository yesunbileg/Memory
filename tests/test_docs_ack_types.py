import pytest

from ai_memory.web_docs_protocol import confirmed_revision


@pytest.mark.parametrize(
    ("server", "start", "end"),
    [(False, 1, 1), (0.0, 1, 1), (0, True, 1), (0, 1, 1.0)],
)
def test_revision_ack_requires_json_integers(server, start, end):
    ack = {
        "metadata": {"needsTransformOnClient": False, "serverRevision": server},
        "revisionRanges": [[start, end]],
    }
    assert confirmed_revision(ack, 0) is None

"""Contract tests for the deterministic layer around the classifier.

These exercise the rules the README claims the service enforces, without
depending on live model output or network access:

  - the model's proposed topics are reduced to the closed vocabulary
  - UNRELATED carries no relevance topics
  - confidence is clamped into [0, 1]
  - malformed model output is retried exactly once, then fails structurally
  - neither retrieval path hands structured data to the classifier
  - a connection landing on a private address is refused, whatever DNS said
  - the total request budget is enforced even when a stage timeout does not fire
  - topics survive persistence round-trip, commas included

Stdlib only, deliberately small. Run with:  python test_contract.py
"""

import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from app import classifier, db, fetcher
from app.classifier import _parse, classify_article


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------

class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _FakeGroq:
    """Returns each queued payload in turn, recording how many calls were made."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = 0
        self.chat = self  # so .chat.completions.create resolves back here
        self.completions = self

    def with_options(self, **_kwargs):
        return self

    async def create(self, **_kwargs):
        self.calls += 1
        if not self._payloads:
            raise AssertionError("classifier made more attempts than expected")
        return _Response(self._payloads.pop(0))


VALID_JSON = (
    '{"label": "GOOD_NEWS", "confidence": 0.8, "reasoning": "Relevant.",'
    ' "relevance_topics": ["wealth_management_software"]}'
)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_off_vocabulary_topics_are_discarded():
    result = _parse(
        '{"label": "GOOD_NEWS", "confidence": 0.8, "reasoning": "Relevant.",'
        ' "relevance_topics": ["wealth management software", "random unrelated topic"]}'
    )
    assert result["relevance_topics"] == ["wealth_management_software"], result


def test_topic_normalization_handles_case_and_separators():
    result = _parse(
        '{"label": "BAD_NEWS", "confidence": 0.5, "reasoning": "Relevant.",'
        ' "relevance_topics": ["Regulation", "AI-in-financial-workflows", "regulation"]}'
    )
    # Normalized, de-duplicated, order preserved.
    assert result["relevance_topics"] == ["regulation", "ai_in_financial_workflows"], result


def test_unrelated_carries_no_topics():
    result = _parse(
        '{"label": "UNRELATED", "confidence": 0.9, "reasoning": "Not relevant.",'
        ' "relevance_topics": ["regulation"]}'
    )
    assert result["relevance_topics"] == [], result


def test_confidence_is_clamped():
    high = _parse('{"label": "GOOD_NEWS", "confidence": 4.2, "reasoning": "x", "relevance_topics": []}')
    low = _parse('{"label": "GOOD_NEWS", "confidence": -1, "reasoning": "x", "relevance_topics": []}')
    junk = _parse('{"label": "GOOD_NEWS", "confidence": "high", "reasoning": "x", "relevance_topics": []}')
    assert high["confidence"] == 1.0, high
    assert low["confidence"] == 0.0, low
    assert junk["confidence"] == 0.5, junk


def test_invalid_label_is_rejected():
    assert _parse('{"label": "MAYBE", "confidence": 0.5, "reasoning": "x"}') is None


def test_retry_recovers_from_one_malformed_response(monkeypatched):
    fake = _FakeGroq(["not json at all", VALID_JSON])
    monkeypatched(fake)
    result = asyncio.run(classify_article("T", "body text", budget=30))
    assert result["label"] == "GOOD_NEWS", result
    assert fake.calls == 2, f"expected exactly one retry, got {fake.calls} attempts"


def test_two_malformed_responses_fail_structurally(monkeypatched):
    fake = _FakeGroq(["nope", "still nope"])
    monkeypatched(fake)
    result = asyncio.run(classify_article("T", "body text", budget=30))
    assert result.get("error") == "classification_failed", result
    assert fake.calls == 2, f"expected exactly two attempts, got {fake.calls}"


def test_exhausted_budget_skips_the_call(monkeypatched):
    fake = _FakeGroq([VALID_JSON])
    monkeypatched(fake)
    result = asyncio.run(classify_article("T", "body text", budget=0.1))
    assert result.get("error") == "classification_failed", result
    assert fake.calls == 0, "should not call the provider with no budget left"
    # The provider was never asked, so the detail must not report a bad answer.
    assert "budget" in result["detail"].lower(), result
    assert "parseable" not in result["detail"].lower(), result


def test_topics_survive_persistence_including_commas():
    with tempfile.TemporaryDirectory() as tmp:
        original = db.DB_PATH
        db.DB_PATH = Path(tmp) / "test.db"
        try:
            db.init_db()
            topics = ["regulation", "a topic, with a comma"]
            db.insert_classification(
                "https://example.com/a", "GOOD_NEWS", 0.7, "why", topics,
                datetime.now(timezone.utc),
            )
            row = db.get_latest(1)[0]
            assert row["relevance_topics"] == topics, row
            assert row["confidence"] == 0.7, row
            assert row["processed_at"].endswith("Z"), row
        finally:
            db.DB_PATH = original


# --------------------------------------------------------------------------
# Machine-payload gate
#
# The risk here is false positives, not false negatives: wrongly rejecting a
# real article is worse than letting an odd payload through, so the accept
# cases outnumber the reject case deliberately.
# --------------------------------------------------------------------------

JSON_PAYLOAD = """{ "args": {}, "data": "", "files": {}, "form": {}, "headers": {
"Accept": "text/html,application/xhtml+xml", "Host": "httpbin.org",
"User-Agent": "Mozilla/5.0", "X-Amzn-Trace-Id": "Root=1-abc-def" },
"origin": "10.0.0.1", "url": "https://httpbin.org/delay/12" }"""

ARTICLE_TEXT = (
    "WealthAi has launched a platform for independent financial advisers. The system "
    "unifies meeting notes, CRM records and compliance oversight in one place. During "
    "beta trials, firms reported that routine client administration time fell by around "
    "60 percent. The company said the launch follows two years of development with "
    "advisory firms in the United Kingdom and Ireland."
)

INDEX_TEXT = (
    "Skip to content BBC Sport Home Football Cricket Formula 1 Rugby Union Tennis Golf "
    "Athletics Live scores Fixtures Tables Gossip Latest news Watch highlights More from "
    "the BBC Weather Sounds iPlayer News Sport Business Innovation Culture Travel Earth"
)

# An article about data integration that quotes a payload. This is the case the
# AND condition exists for: it trips the key/value signal on its own.
TECHNICAL_ARTICLE = (
    "Custodian connectivity remains one of the harder integration problems in wealth "
    "management. Most custodians still expose positions over nightly batch files, and "
    "firms modernising legacy systems must normalise those into a common schema before "
    "portfolio analytics can run. A typical normalised holding looks like this: "
    '{ "accountId": "A-1029", "isin": "IE00B4L5Y983", "quantity": 1450, '
    '"currency": "EUR", "asOf": "2026-09-01" } '
    "Firms that adopt a shared representation early report materially lower reconciliation "
    "costs later. The alternative is a bespoke mapping per custodian, which becomes "
    "expensive to maintain as the number of connected institutions grows over time."
)


def test_machine_payload_is_detected():
    assert fetcher.looks_like_machine_payload(JSON_PAYLOAD)


def test_real_article_is_not_machine_payload():
    assert not fetcher.looks_like_machine_payload(ARTICLE_TEXT)


def test_index_page_is_not_machine_payload():
    assert not fetcher.looks_like_machine_payload(INDEX_TEXT)


def test_technical_article_quoting_json_is_not_machine_payload():
    """The false-positive guard, and the reason both signals must fire.

    This text trips the key/value threshold on its own; only the structural
    punctuation share keeps it classified as prose. An OR condition here would
    reject a legitimate article about data integration -- a theme the brief
    explicitly lists as relevant.
    """
    kv = len(re.findall(r'"[A-Za-z_][A-Za-z0-9_]*"\s*:', TECHNICAL_ARTICLE))
    share = sum(TECHNICAL_ARTICLE.count(c) for c in '{}[]":,') / len(TECHNICAL_ARTICLE)

    assert kv >= fetcher.MACHINE_KV_PAIRS, f"fixture should trip the kv signal, got {kv}"
    assert share <= fetcher.MACHINE_STRUCTURAL_SHARE, f"structural share {share:.3f}"
    assert not fetcher.looks_like_machine_payload(TECHNICAL_ARTICLE)


def test_empty_text_is_not_machine_payload():
    assert not fetcher.looks_like_machine_payload("")


def test_neither_retrieval_path_accepts_machine_data():
    """Path parity: the architectural invariant, not a per-URL assertion."""
    for source in ("direct", "reader"):
        candidate = fetcher.Article(title="x", text=JSON_PAYLOAD, source=source)
        rejected = fetcher.reject_if_machine_payload(candidate)
        assert rejected is not None, source
        assert rejected.error == "unsupported_content_type", (source, rejected)

    # A real article is rejected by neither path, and an upstream FetchError is
    # passed through untouched rather than relabelled.
    assert fetcher.reject_if_machine_payload(
        fetcher.Article(title="x", text=ARTICLE_TEXT, source="direct")
    ) is None
    assert fetcher.reject_if_machine_payload(
        fetcher.FetchError(error="http_error", detail="HTTP 404")
    ) is None


class _FakeStream:
    """Stands in for the transport's network stream, which reports the peer."""

    def __init__(self, server_addr):
        self._server_addr = server_addr

    def get_extra_info(self, name):
        return self._server_addr if name == "server_addr" else None


class _FakeResponse:
    def __init__(self, extensions):
        self.extensions = extensions


def test_peer_address_check_blocks_a_private_connection():
    """DNS rebinding: the pre-flight check and the connection are two lookups.

    A hostname can resolve to a public address for validate_url and a private
    one for the socket that follows, so the address is checked again on the
    connection that was actually opened.
    """
    for address in [("127.0.0.1", 80), ("169.254.169.254", 80), ("10.0.0.5", 443)]:
        rejected = fetcher.reject_unsafe_peer(_FakeResponse({"network_stream": _FakeStream(address)}))
        assert rejected is not None, address
        assert rejected.error == "blocked_url", (address, rejected)


def test_peer_address_check_allows_public_and_fails_open():
    public = _FakeResponse({"network_stream": _FakeStream(("185.15.59.224", 443))})
    assert fetcher.reject_unsafe_peer(public) is None

    # With no transport detail available the check contributes nothing and
    # validate_url remains the guarantee, rather than every request failing.
    assert fetcher.reject_unsafe_peer(_FakeResponse({})) is None
    assert fetcher.reject_unsafe_peer(_FakeResponse({"network_stream": _FakeStream(None)})) is None


def test_global_budget_backstop_returns_request_timeout():
    """The wait_for ceiling, not a stage timeout.

    In practice a stage timeout almost always fires first (a hanging fetch
    returns fetch_failed at ~10s). This proves the outer guarantee still holds
    for anything that slips past them.
    """
    # Deferred: importing app.main constructs the FastAPI app and calls
    # load_dotenv(), neither of which the rest of this offline suite needs.
    from app import main

    real_pipeline, real_budget = main._run_pipeline, main.TOTAL_REQUEST_BUDGET

    async def never_finishes(_url):
        await asyncio.sleep(10)

    main._run_pipeline = never_finishes
    main.TOTAL_REQUEST_BUDGET = 0.5
    try:
        asyncio.run(main.classify(main.ClassifyRequest(url="https://example.com/a")))
    except HTTPException as e:
        assert e.status_code == 504, e.status_code
        assert e.detail["error"] == "request_timeout", e.detail
    else:
        raise AssertionError("expected the budget backstop to fire")
    finally:
        main._run_pipeline, main.TOTAL_REQUEST_BUDGET = real_pipeline, real_budget


def test_legacy_comma_rows_still_read():
    assert db._load_topics("regulation,compliance_reporting") == [
        "regulation", "compliance_reporting",
    ]
    assert db._load_topics("") == []


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def main() -> int:
    def monkeypatched(fake):
        classifier.AsyncGroq = lambda **_kwargs: fake

    real_groq = classifier.AsyncGroq
    os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")

    passed, failed = 0, []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            if fn.__code__.co_argcount:
                fn(monkeypatched)
            else:
                fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            failed.append(name)
        finally:
            classifier.AsyncGroq = real_groq

    print(f"\n{passed} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    print("Contract tests\n" + "=" * 50)
    sys.exit(main())

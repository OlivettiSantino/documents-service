"""Contract with extract-service: the shape of ``POST /extract`` responses.

``extract_response.json`` mirrors ``ExtractResponse`` in extract-service
(``extractor/api/schemas.py``): ``content: str`` and ``page_count: int >= 0``.
If extract changes that schema, this fixture has to change with it, and then
these tests say whether our parser still accepts it.

Only ``_parse`` is exercised, not the HTTP call or the resilience layers: the
contract is about the payload, and that keeps these tests independent of how
the client reaches extract.
"""

import json
from pathlib import Path

import httpx
import pytest

from app.exceptions import ExtractProtocolError
from app.services.extractor_client import ExtractorClient

FIXTURE = Path(__file__).parent / "extract_response.json"


@pytest.fixture
def contract() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def parse(payload) -> object:
    # `_parse` does not touch instance state; skipping __init__ keeps the test
    # from depending on the constructor's collaborators.
    client = ExtractorClient.__new__(ExtractorClient)
    return client._parse(httpx.Response(200, json=payload))


def test_the_fixture_matches_extract_schema(contract):
    """Guards the fixture itself against drifting from ExtractResponse."""
    assert set(contract) == {"content", "page_count"}
    assert isinstance(contract["content"], str)
    assert isinstance(contract["page_count"], int) and contract["page_count"] >= 0


def test_a_valid_response_is_accepted(contract):
    result = parse(contract)

    assert result.content == contract["content"]
    assert result.page_count == contract["page_count"]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"page_count": 0}, id="zero-pages"),
        pytest.param({"content": ""}, id="empty-content"),
        pytest.param({"extra": "campo nuevo"}, id="unknown-field-is-ignored"),
    ],
)
def test_edge_cases_allowed_by_the_schema_are_accepted(contract, overrides):
    result = parse({**contract, **overrides})

    assert result.page_count == {**contract, **overrides}["page_count"]


def _without(contract: dict, key: str) -> dict:
    return {k: v for k, v in contract.items() if k != key}


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda c: _without(c, "content"), id="missing-content"),
        pytest.param(lambda c: _without(c, "page_count"), id="missing-page_count"),
        pytest.param(lambda c: {**c, "content": None}, id="null-content"),
        pytest.param(lambda c: {**c, "content": 123}, id="content-not-str"),
        pytest.param(lambda c: {**c, "page_count": "3"}, id="page_count-as-str"),
        pytest.param(lambda c: {**c, "page_count": 3.0}, id="page_count-as-float"),
        pytest.param(lambda c: {**c, "page_count": True}, id="page_count-as-bool"),
        pytest.param(lambda c: {**c, "page_count": -1}, id="negative-page_count"),
        pytest.param(lambda c: [c], id="not-an-object"),
    ],
)
def test_invalid_variants_are_rejected(contract, mutate):
    with pytest.raises(ExtractProtocolError):
        parse(mutate(contract))

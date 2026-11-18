"""Claude Vision wrapper — the Anthropic client is mocked; no API calls are made.

These tests cover the wrapper's contract (request shape, error → VisionParseError),
not extraction quality. Receipt-reading accuracy is exercised separately by
tests/manual_vision_check.py against the real API.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest

from models.schemas import ReceiptExtraction
from services import vision
from services.vision import VisionParseError


@pytest.fixture
def anthropic_client(monkeypatch):
    client = MagicMock(name="Anthropic")
    monkeypatch.setattr(vision, "_get_client", lambda: client)
    return client


def _resp(parsed=None, stop_reason="end_turn"):
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason)


def test_success_returns_parsed_extraction(anthropic_client):
    expected = ReceiptExtraction(vendor="Cafe", total=9.99)
    anthropic_client.messages.parse.return_value = _resp(expected)
    assert vision.extract_receipt(b"img", "image/png") is expected


def test_request_shape(anthropic_client):
    anthropic_client.messages.parse.return_value = _resp(ReceiptExtraction())
    vision.extract_receipt(b"img-bytes", "image/png")

    kwargs = anthropic_client.messages.parse.call_args.kwargs
    assert kwargs["model"] == vision._MODEL
    assert kwargs["output_format"] is ReceiptExtraction
    [msg] = kwargs["messages"]
    image_block, text_block = msg["content"]
    assert image_block["source"] == {
        "type": "base64", "media_type": "image/png",
        "data": "aW1nLWJ5dGVz",  # base64("img-bytes")
    }
    assert text_block["type"] == "text" and "receipt" in text_block["text"].lower()


def test_prompt_categories_match_modal_categories():
    # The modal only pre-selects a category if Claude returns one of these exact
    # values, so the prompt and the modal must agree.
    from app.views.modals import CATEGORIES
    for c in CATEGORIES:
        assert c in vision._PROMPT


def test_api_error_becomes_vision_parse_error(anthropic_client):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    anthropic_client.messages.parse.side_effect = anthropic.APIConnectionError(request=req)
    with pytest.raises(VisionParseError):
        vision.extract_receipt(b"img", "image/png")


def test_refusal_becomes_vision_parse_error(anthropic_client):
    anthropic_client.messages.parse.return_value = _resp(None, stop_reason="refusal")
    with pytest.raises(VisionParseError):
        vision.extract_receipt(b"img", "image/png")


def test_missing_parsed_output_becomes_vision_parse_error(anthropic_client):
    anthropic_client.messages.parse.return_value = _resp(None, stop_reason="max_tokens")
    with pytest.raises(VisionParseError):
        vision.extract_receipt(b"img", "image/png")

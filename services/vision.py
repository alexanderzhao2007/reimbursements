"""Claude Vision receipt parsing.

Sends the receipt image to Claude (claude-sonnet-5) using structured outputs:
`messages.parse()` with `output_format=ReceiptExtraction` constrains the model to
return JSON that matches the schema by construction, so there is no markdown-fence
stripping or manual JSON parsing. Callers treat VisionParseError as the signal to
fall back to manual entry (a blank modal), per ARCHITECTURE.md's error strategy.
"""

import logging

import anthropic

from models.schemas import ReceiptExtraction
from utils import config
from utils.image import encode_base64

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-5"
_MAX_TOKENS = 1024  # the JSON payload is small; no streaming needed

_PROMPT = """You are a receipt parser. Analyze this receipt image and extract the
structured data defined by the response schema.

Rules:
- Return null for ANY field you cannot read clearly. Never guess.
- "total" is the final amount paid including tax and tip.
- "category" should be one of: meals, travel, supplies, software, equipment, lodging, other.
- "date" must be ISO 8601 format (YYYY-MM-DD).
- If the receipt is not in English, still extract the data and translate the vendor name."""

_client: "anthropic.Anthropic | None" = None


class VisionParseError(Exception):
    """Raised when the receipt could not be parsed into a ReceiptExtraction.
    Callers fall back to opening a blank modal for manual entry."""


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def extract_receipt(image_bytes: bytes, media_type: str) -> ReceiptExtraction:
    """Parse a receipt image with Claude Vision using structured outputs.

    `media_type` must be a Claude-supported image type (image/jpeg, image/png,
    image/gif, image/webp) — the caller converts unsupported formats to PNG
    first. Raises VisionParseError on API failure, refusal, or an empty/invalid
    structured result so the pipeline can fall back to manual entry.
    """
    b64 = encode_base64(image_bytes)
    try:
        resp = _get_client().messages.parse(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            output_format=ReceiptExtraction,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": _PROMPT},
                    ],
                }
            ],
        )
    except anthropic.APIError as e:
        logger.warning("Claude Vision API error: %s", e)
        raise VisionParseError("Claude Vision API error") from e

    if resp.stop_reason == "refusal":
        logger.warning("Claude Vision refused the request")
        raise VisionParseError("Claude Vision refused the request")

    # A refusal or truncated (max_tokens) response leaves parsed_output unset.
    if resp.parsed_output is None:
        logger.warning(
            "Claude Vision returned no structured output (stop_reason=%s)",
            resp.stop_reason,
        )
        raise VisionParseError("no structured output from Claude Vision")

    return resp.parsed_output

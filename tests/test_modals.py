"""Block Kit modal builder."""

import json

from app.views.modals import (CALLBACK_ID, CATEGORIES, build_modal, decode_metadata,
                              encode_metadata)
from models.schemas import ReceiptExtraction


def _blocks_by_id(view):
    return {b["block_id"]: b for b in view["blocks"] if "block_id" in b}


def test_modal_envelope():
    view = build_modal("sub-1", ReceiptExtraction())
    assert view["type"] == "modal"
    assert view["callback_id"] == CALLBACK_ID == "reimbursement_modal"
    assert decode_metadata(view["private_metadata"]) == ("sub-1", None, None)
    assert len(view["title"]["text"]) <= 24          # Slack limit
    assert len(view["submit"]["text"]) <= 24


def test_metadata_carries_review_message_location():
    view = build_modal("sub-1", ReceiptExtraction(), channel="D1", ts="123.456")
    assert decode_metadata(view["private_metadata"]) == ("sub-1", "D1", "123.456")
    assert len(view["private_metadata"]) <= 3000     # Slack limit


def test_metadata_round_trip():
    assert decode_metadata(encode_metadata("a", "b", "c")) == ("a", "b", "c")


def test_legacy_bare_submission_id_metadata_still_decodes():
    # Modals opened before the JSON format shipped carry only the bare UUID.
    sid = "0b6f6c1e-8f3a-4d0e-9a51-2f1b9c7d4e10"
    assert decode_metadata(sid) == (sid, None, None)
    assert decode_metadata(json.dumps(["not", "a", "dict"]))[1:] == (None, None)


def test_block_ids_match_action_ids():
    # modal_submit reads state as values[block_id][action_id]; they must agree.
    for block_id, block in _blocks_by_id(build_modal("s", ReceiptExtraction())).items():
        assert block["element"]["action_id"] == block_id


def test_prefilled_from_extraction():
    view = build_modal("s", ReceiptExtraction(
        vendor="Cafe", date="2026-03-14", total=12.5, currency="EUR",
        category="meals", tax=1.0,
    ))
    b = _blocks_by_id(view)
    assert b["vendor"]["element"]["initial_value"] == "Cafe"
    assert b["date"]["element"]["initial_date"] == "2026-03-14"
    assert b["total"]["element"]["initial_value"] == "12.5"
    assert b["currency"]["element"]["initial_value"] == "EUR"
    assert b["category"]["element"]["initial_option"]["value"] == "meals"
    assert b["tax"]["element"]["initial_value"] == "1.0"


def test_blank_extraction_has_no_initial_values_except_currency():
    b = _blocks_by_id(build_modal("s", ReceiptExtraction()))
    for field in ("vendor", "total", "tax"):
        assert "initial_value" not in b[field]["element"]
    assert "initial_date" not in b["date"]["element"]
    assert "initial_option" not in b["category"]["element"]
    assert b["currency"]["element"]["initial_value"] == "USD"


def test_currency_none_falls_back_to_usd():
    b = _blocks_by_id(build_modal("s", ReceiptExtraction(currency=None)))
    assert b["currency"]["element"]["initial_value"] == "USD"


def test_zero_total_is_still_prefilled():
    b = _blocks_by_id(build_modal("s", ReceiptExtraction(total=0.0)))
    assert b["total"]["element"]["initial_value"] == "0.0"


def test_unknown_category_is_not_preselected():
    # Slack rejects an initial_option that isn't among the options.
    b = _blocks_by_id(build_modal("s", ReceiptExtraction(category="Food & Drink")))
    assert "initial_option" not in b["category"]["element"]


def test_required_and_optional_fields():
    b = _blocks_by_id(build_modal("s", ReceiptExtraction()))
    assert b["tax"]["optional"] is True
    for field in ("vendor", "date", "total", "currency", "category"):
        assert b[field]["optional"] is False


def test_category_options_cover_all_categories():
    b = _blocks_by_id(build_modal("s", ReceiptExtraction()))
    assert [o["value"] for o in b["category"]["element"]["options"]] == CATEGORIES


def test_line_items_rendered_read_only():
    view = build_modal("s", ReceiptExtraction(
        line_items=[{"description": "Latte", "amount": 5, "quantity": 2}],
    ))
    section = view["blocks"][-1]
    assert section["type"] == "section"
    assert "Latte" in section["text"]["text"] and "×2" in section["text"]["text"]


def test_no_line_items_placeholder():
    section = build_modal("s", ReceiptExtraction())["blocks"][-1]
    assert "No line items" in section["text"]["text"]

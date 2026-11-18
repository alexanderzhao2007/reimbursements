"""Pydantic model validation."""

from datetime import date

import pytest
from pydantic import ValidationError

from models.schemas import LineItem, ReceiptExtraction, Submission


def test_extraction_all_fields_default_to_null_except_currency():
    e = ReceiptExtraction()
    assert e.vendor is None
    assert e.date is None
    assert e.total is None
    assert e.category is None
    assert e.tax is None
    assert e.line_items is None
    assert e.currency == "USD"


def test_extraction_parses_iso_date_string_into_date():
    # Regression guard: `date` field name must not shadow the `date` type.
    e = ReceiptExtraction(date="2026-03-14")
    assert e.date == date(2026, 3, 14)


def test_extraction_rejects_malformed_date():
    with pytest.raises(ValidationError):
        ReceiptExtraction(date="14/03/2026")


def test_extraction_coerces_numeric_strings():
    e = ReceiptExtraction(total="42.50", tax="3")
    assert e.total == 42.5
    assert e.tax == 3.0


def test_extraction_rejects_non_numeric_total():
    with pytest.raises(ValidationError):
        ReceiptExtraction(total="forty two")


def test_line_items_from_dicts_and_default_quantity():
    e = ReceiptExtraction(line_items=[{"description": "Coffee", "amount": 4.5}])
    assert e.line_items == [LineItem(description="Coffee", amount=4.5, quantity=1)]


def test_line_item_requires_description_and_amount():
    with pytest.raises(ValidationError):
        LineItem(description="no amount")
    with pytest.raises(ValidationError):
        LineItem(amount=1.0)


def test_extraction_json_dump_is_db_safe():
    e = ReceiptExtraction(date="2026-03-14", total=10, line_items=[{"description": "x", "amount": 1}])
    data = e.model_dump(mode="json")
    assert data["date"] == "2026-03-14"
    assert data["line_items"] == [{"description": "x", "amount": 1.0, "quantity": 1}]


def test_submission_defaults():
    s = Submission(
        submission_id="id", user_id="U1", file_id="F1", image_hash="h",
        extraction=ReceiptExtraction(), created_at="2026-01-01T00:00:00Z",
    )
    assert s.status == "pending"
    assert s.reimbursed is False
    assert s.reimbursed_at is None


def test_submission_has_no_department_field():
    # It's a club, not a corporation — department was intentionally dropped.
    assert "department" not in Submission.model_fields

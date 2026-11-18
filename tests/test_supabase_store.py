"""Supabase data access against an in-memory fake (see conftest.FakeSupabase)."""

import pytest
from postgrest.exceptions import APIError

from models.schemas import ReceiptExtraction
from services import supabase_store
from services.supabase_store import DuplicateSubmission


def _insert(sid="sub-1", image_hash="hash-1", user_id="U1", extraction=None):
    supabase_store.insert_pending(
        submission_id=sid, user_id=user_id, file_id="F1", image_hash=image_hash,
        extraction=extraction or ReceiptExtraction(vendor="Cafe", total=12.5, date="2026-03-14"),
    )


def test_insert_pending_writes_row_with_flattened_extraction(fake_db):
    _insert(extraction=ReceiptExtraction(
        vendor="Cafe", date="2026-03-14", total=12.5, category="meals",
        line_items=[{"description": "Latte", "amount": 5}],
    ))
    [row] = fake_db.rows()
    assert row["submission_id"] == "sub-1"
    assert row["status"] == "pending"
    assert row["date"] == "2026-03-14"           # JSON-safe string, not a date object
    assert row["line_items"] == [{"description": "Latte", "amount": 5.0, "quantity": 1}]
    assert "department" not in row


def test_get_returns_row_or_none(fake_db):
    _insert()
    assert supabase_store.get("sub-1")["vendor"] == "Cafe"
    assert supabase_store.get("missing") is None


def test_find_by_hash(fake_db):
    assert supabase_store.find_by_hash("hash-1") is None
    _insert()
    row = supabase_store.find_by_hash("hash-1")
    assert row["submission_id"] == "sub-1"
    assert row["status"] == "pending" and row["user_id"] == "U1"
    assert row["vendor"] == "Cafe"   # full row, so it can pre-fill a re-offered modal


def test_insert_duplicate_hash_raises_with_existing_row(fake_db):
    _insert(sid="first")
    with pytest.raises(DuplicateSubmission) as exc:
        _insert(sid="second")
    assert exc.value.submission_id == "first"
    assert exc.value.existing["status"] == "pending"
    assert len(fake_db.rows()) == 1


def test_duplicate_submission_tolerates_missing_row():
    exc = DuplicateSubmission(None)
    assert exc.existing is None and exc.submission_id is None


def test_insert_non_unique_api_error_propagates(fake_db):
    fake_db.fail_next = APIError({"message": "boom", "code": "42P01"})
    with pytest.raises(APIError):
        _insert()


def test_extraction_round_trips_through_row(fake_db):
    original = ReceiptExtraction(
        vendor="Cafe", date="2026-03-14", total=12.5, currency="EUR",
        category="meals", tax=1.25, line_items=[{"description": "Latte", "amount": 5, "quantity": 2}],
    )
    _insert(extraction=original)
    assert supabase_store.extraction_from_row(supabase_store.get("sub-1")) == original


def test_extraction_from_row_handles_postgres_numeric_strings():
    # PostgREST can serialize numeric(12,2) as strings; the model must still load.
    e = supabase_store.extraction_from_row({"total": "12.50", "tax": "1.00", "date": "2026-03-14"})
    assert e.total == 12.5 and e.tax == 1.0


def test_confirm_updates_row_and_maps_email_column(fake_db):
    _insert()
    edited = ReceiptExtraction(vendor="Cafe Edited", date="2026-03-15", total=20, category="meals")
    row = supabase_store.confirm(
        submission_id="sub-1", extraction=edited,
        employee_name="Ada", employee_email="ada@example.com",
    )
    assert row["status"] == "confirmed"
    assert row["vendor"] == "Cafe Edited"
    assert row["date"] == "2026-03-15"
    assert row["email"] == "ada@example.com"
    assert "employee_email" not in row
    assert row["submitted_at"]
    # image_hash / user_id must not be clobbered by the update
    assert row["image_hash"] == "hash-1" and row["user_id"] == "U1"


def test_confirm_never_touches_reimbursed_flag(fake_db):
    _insert()
    supabase_store.confirm(submission_id="sub-1", extraction=ReceiptExtraction(total=1),
                           employee_name=None, employee_email=None)
    _, _, payload, _ = fake_db.calls[-1]
    assert "reimbursed" not in payload and "reimbursed_at" not in payload


def test_confirm_is_guarded_by_status(fake_db):
    _insert()
    supabase_store.confirm(submission_id="sub-1", extraction=ReceiptExtraction(total=1),
                           employee_name=None, employee_email=None)
    _, op, _, filter_cols = fake_db.calls[-1]
    assert op == "update" and "status" in filter_cols


def test_second_confirm_is_rejected_and_does_not_overwrite(fake_db):
    _insert()
    kw = dict(submission_id="sub-1", employee_name=None, employee_email=None)
    assert supabase_store.confirm(extraction=ReceiptExtraction(total=1), **kw)
    assert supabase_store.confirm(extraction=ReceiptExtraction(total=999), **kw) is None
    assert supabase_store.get("sub-1")["total"] == 1


def test_errored_row_can_be_confirmed_on_retry(fake_db):
    _insert()
    supabase_store.mark_error("sub-1")
    row = supabase_store.confirm(submission_id="sub-1", extraction=ReceiptExtraction(total=5),
                                 employee_name=None, employee_email=None)
    assert row["status"] == "confirmed"


def test_confirm_unknown_id_returns_none(fake_db):
    assert supabase_store.confirm(submission_id="nope", extraction=ReceiptExtraction(),
                                  employee_name=None, employee_email=None) is None


def test_mark_error(fake_db):
    _insert()
    supabase_store.mark_error("sub-1")
    assert supabase_store.get("sub-1")["status"] == "error"


def test_list_by_user_filters_and_orders_newest_first(fake_db):
    _insert(sid="a", image_hash="h1", user_id="U1")
    _insert(sid="b", image_hash="h2", user_id="U2")
    _insert(sid="c", image_hash="h3", user_id="U1")
    assert [r["submission_id"] for r in supabase_store.list_by_user("U1")] == ["c", "a"]
    assert supabase_store.list_by_user("U1", limit=1)[0]["submission_id"] == "c"

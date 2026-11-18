"""Review-button → modal → submit flow, against the in-memory Supabase fake."""

from unittest.mock import MagicMock

import pytest

from app.listeners import modal_submit
from app.views.modals import decode_metadata, encode_metadata
from models.schemas import ReceiptExtraction
from services import slack_helpers, supabase_store
from tests.conftest import posted_texts
from tests.test_file_upload import FakeApp


@pytest.fixture
def handlers(inline_threads):
    app = FakeApp()
    modal_submit.register(app)
    return app.actions["open_review_modal"], app.views["reimbursement_modal"]


@pytest.fixture
def pending(fake_db):
    supabase_store.insert_pending(
        submission_id="sub-1", user_id="U1", file_id="F1", image_hash="h1",
        extraction=ReceiptExtraction(
            vendor="Cafe", date="2026-03-14", total=12.5, category="meals",
            line_items=[{"description": "Latte", "amount": 5}],
        ),
    )
    return fake_db


def _view(sid="sub-1", vendor="Cafe", date="2026-03-14", total="12.50",
          currency="USD", category="meals", tax=None):
    return {
        "private_metadata": encode_metadata(sid, "D1", "111.222"),
        "state": {"values": {
            "vendor": {"vendor": {"value": vendor}},
            "date": {"date": {"selected_date": date}},
            "total": {"total": {"value": total}},
            "currency": {"currency": {"value": currency}},
            "category": {"category": {"selected_option": {"value": category} if category else None}},
            "tax": {"tax": {"value": tax}},
        }},
    }


def _submit(handler, client, **view_kw):
    ack = MagicMock()
    view = _view(**view_kw)
    handler(ack=ack, body={"user": {"id": "U1"}, "view": view}, view=view, client=client)
    return ack


# --- _parse_money -------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("12.50", 12.5),
    ("$1,234.56", 1234.56),
    ("  7 ", 7.0),
    ("12.345", 12.35),        # rounded to cents, matching numeric(12,2)
    ("0", 0.0),               # zero is fine for tax
    ("", None),
    ("   ", None),
    (None, None),
])
def test_parse_money_valid(raw, expected):
    assert modal_submit._parse_money(raw) == (expected, None)


@pytest.mark.parametrize("raw,msg", [
    ("twelve", "valid number"),
    ("nan", "valid number"),
    ("NaN", "valid number"),
    ("inf", "valid number"),
    ("-inf", "valid number"),
    ("-50", "negative"),
    ("-0.01", "negative"),
    ("10000000000", "too large"),   # 11 integer digits overflow numeric(12,2)
    ("1e20", "too large"),
])
def test_parse_money_rejects_nonsense_amounts(raw, msg):
    value, error = modal_submit._parse_money(raw)
    assert value is None and msg in error


def test_parse_money_zero_rejected_when_disallowed():
    value, error = modal_submit._parse_money("0", allow_zero=False)
    assert value is None and "greater than zero" in error


def test_largest_allowed_amount_fits_column():
    assert modal_submit._parse_money("9999999999.99") == (9999999999.99, None)


# --- open_review_modal --------------------------------------------------------

def _click(handler, client, sid="sub-1"):
    ack = MagicMock()
    handler(ack=ack, client=client, body={
        "actions": [{"value": sid}], "trigger_id": "T1", "user": {"id": "U1"},
        "channel": {"id": "D1"}, "message": {"ts": "111.222"},
    })
    return ack


def _finance_posts(client):
    return [c for c in client.chat_postMessage.call_args_list
            if c.kwargs.get("channel") == "CFINANCE"]


def test_button_opens_modal_prefilled_from_row(handlers, pending, slack_client):
    open_modal, _ = handlers
    _click(open_modal, slack_client).assert_called_once()
    kwargs = slack_client.views_open.call_args.kwargs
    assert kwargs["trigger_id"] == "T1"
    view = kwargs["view"]
    assert decode_metadata(view["private_metadata"]) == ("sub-1", "D1", "111.222")
    vendor_block = next(b for b in view["blocks"] if b.get("block_id") == "vendor")
    assert vendor_block["element"]["initial_value"] == "Cafe"


def test_button_with_missing_row_tells_user_to_reupload(handlers, fake_db, slack_client):
    open_modal, _ = handlers
    _click(open_modal, slack_client, sid="gone")
    slack_client.views_open.assert_not_called()
    assert "re-upload" in posted_texts(slack_client)[-1]


def test_button_on_confirmed_submission_does_not_open_modal(handlers, pending, slack_client):
    open_modal, submit = handlers
    _submit(submit, slack_client)
    slack_client.reset_mock()

    _click(open_modal, slack_client)
    slack_client.views_open.assert_not_called()
    assert "already submitted" in posted_texts(slack_client)[-1]
    # ...and the stale button is replaced
    assert slack_client.chat_update.call_args.kwargs["ts"] == "111.222"


def test_button_on_errored_submission_opens_modal_for_retry(handlers, pending, slack_client):
    supabase_store.mark_error("sub-1")
    open_modal, _ = handlers
    _click(open_modal, slack_client)
    slack_client.views_open.assert_called_once()


def test_cannot_resubmit_confirmed_submission(handlers, pending, slack_client):
    # Two modals opened before either was submitted, or a double click.
    _, submit = handlers
    _submit(submit, slack_client)
    assert len(_finance_posts(slack_client)) == 1
    _submit(submit, slack_client, total="999.00")
    assert len(_finance_posts(slack_client)) == 1
    assert supabase_store.get("sub-1")["total"] == 12.5
    assert "already submitted" in posted_texts(slack_client)[-1]


def test_retry_after_error_confirms_and_notifies(handlers, pending, slack_client):
    supabase_store.mark_error("sub-1")
    _, submit = handlers
    _submit(submit, slack_client)
    assert supabase_store.get("sub-1")["status"] == "confirmed"
    assert len(_finance_posts(slack_client)) == 1


# --- reimbursement_modal submit: validation -----------------------------------

@pytest.mark.parametrize("total,msg", [
    (None, "Enter the total"), ("", "Enter the total"), ("abc", "valid number"),
    ("-50", "negative"), ("nan", "valid number"), ("inf", "valid number"),
    ("0", "greater than zero"), ("0.00", "greater than zero"),
    ("99999999999", "too large"),
])
def test_bad_total_returns_field_error(handlers, pending, slack_client, total, msg):
    _, submit = handlers
    ack = _submit(submit, slack_client, total=total)
    kwargs = ack.call_args.kwargs
    assert kwargs["response_action"] == "errors"
    assert msg in kwargs["errors"]["total"]
    assert supabase_store.get("sub-1")["status"] == "pending"
    slack_client.chat_postMessage.assert_not_called()


@pytest.mark.parametrize("tax,msg", [
    ("lots", "valid number"), ("-1", "negative"), ("nan", "valid number"),
    ("20.00", "more than the total"),
])
def test_bad_tax_returns_field_error(handlers, pending, slack_client, tax, msg):
    _, submit = handlers
    ack = _submit(submit, slack_client, total="12.50", tax=tax)
    assert msg in ack.call_args.kwargs["errors"]["tax"]
    assert supabase_store.get("sub-1")["status"] == "pending"


@pytest.mark.parametrize("tax", ["0", "12.50"])
def test_tax_zero_and_equal_to_total_are_allowed(handlers, pending, slack_client, tax):
    _, submit = handlers
    _submit(submit, slack_client, total="12.50", tax=tax).assert_called_once_with()


def test_both_fields_invalid_reports_both(handlers, pending, slack_client):
    _, submit = handlers
    ack = _submit(submit, slack_client, total="-5", tax="abc")
    assert set(ack.call_args.kwargs["errors"]) == {"total", "tax"}


def test_submitted_amounts_are_rounded_to_cents(handlers, pending, slack_client):
    _, submit = handlers
    _submit(submit, slack_client, total="12.345", tax="1.005")
    row = supabase_store.get("sub-1")
    assert row["total"] == 12.35 and row["tax"] == round(1.005, 2)


# --- reimbursement_modal submit: happy path -----------------------------------

def test_submit_confirms_row_notifies_finance_and_user(handlers, pending, slack_client):
    _, submit = handlers
    ack = _submit(submit, slack_client, vendor="Cafe Edited", total="$1,020.00", tax="3.10",
                  category="supplies")
    ack.assert_called_once_with()

    row = supabase_store.get("sub-1")
    assert row["status"] == "confirmed"
    assert row["vendor"] == "Cafe Edited"
    assert row["total"] == 1020.0 and row["tax"] == 3.1
    assert row["category"] == "supplies"
    assert row["employee_name"] == "Ada Lovelace" and row["email"] == "ada@example.com"
    assert row["line_items"] == [{"description": "Latte", "amount": 5.0, "quantity": 1}]

    calls = slack_client.chat_postMessage.call_args_list
    finance = [c for c in calls if c.kwargs["channel"] == "CFINANCE"]
    assert len(finance) == 1
    body = finance[0].kwargs["blocks"][1]["text"]["text"]
    assert "Ada Lovelace" in body and "USD 1020.00" in body and "sub-1" in body
    assert "USD 3.10" in body

    dm = [c for c in calls if c.kwargs["channel"] == "U1"]
    assert "Receipt filed" in dm[0].kwargs["text"]

    # The Review & Submit button is replaced with a "Submitted" note.
    update = slack_client.chat_update.call_args.kwargs
    assert (update["channel"], update["ts"]) == ("D1", "111.222")
    assert "Submitted" in update["text"]
    assert not any(b["type"] == "actions" for b in update["blocks"])


def test_button_replacement_failure_does_not_mark_error(handlers, pending, slack_client):
    slack_client.chat_update.side_effect = RuntimeError("message_not_found")
    _, submit = handlers
    _submit(submit, slack_client)
    assert supabase_store.get("sub-1")["status"] == "confirmed"
    assert "Receipt filed" in posted_texts(slack_client)[-1]


def test_legacy_metadata_submit_still_works(handlers, pending, slack_client):
    # A modal opened before the JSON metadata format: bare submission_id.
    _, submit = handlers
    view = _view()
    view["private_metadata"] = "sub-1"
    submit(ack=MagicMock(), body={"user": {"id": "U1"}}, view=view, client=slack_client)
    assert supabase_store.get("sub-1")["status"] == "confirmed"
    slack_client.chat_update.assert_not_called()   # no location to update


def test_submit_without_profile_uses_user_mention(handlers, pending, slack_client, monkeypatch):
    monkeypatch.setattr(slack_helpers, "get_user_profile",
                        lambda *_: {"employee_name": None, "employee_email": None})
    _, submit = handlers
    _submit(submit, slack_client)
    finance = next(c for c in slack_client.chat_postMessage.call_args_list
                   if c.kwargs["channel"] == "CFINANCE")
    assert "<@U1>" in finance.kwargs["text"]
    assert "Email" not in finance.kwargs["blocks"][1]["text"]["text"]


# --- reimbursement_modal submit: failure path ---------------------------------

def test_store_failure_marks_error_and_tells_user(handlers, pending, slack_client, monkeypatch):
    monkeypatch.setattr(supabase_store, "confirm", MagicMock(side_effect=RuntimeError("db")))
    _, submit = handlers
    _submit(submit, slack_client)
    assert supabase_store.get("sub-1")["status"] == "error"
    assert not any(c.kwargs["channel"] == "CFINANCE"
                   for c in slack_client.chat_postMessage.call_args_list)
    assert "contact finance" in posted_texts(slack_client)[-1]


def test_store_failure_message_offers_retry(handlers, pending, slack_client, monkeypatch):
    monkeypatch.setattr(supabase_store, "confirm", MagicMock(side_effect=RuntimeError("db")))
    _, submit = handlers
    _submit(submit, slack_client)
    assert "retry" in posted_texts(slack_client)[-1]
    slack_client.chat_update.assert_not_called()   # button stays so they can retry


def test_finance_post_failure_keeps_row_confirmed(handlers, pending, slack_client,
                                                  monkeypatch, caplog):
    post = MagicMock(side_effect=RuntimeError("channel_not_found"))
    monkeypatch.setattr(slack_helpers, "post_message", post)
    _, submit = handlers
    _submit(submit, slack_client)

    assert supabase_store.get("sub-1")["status"] == "confirmed"
    assert post.call_count == 2                                    # retried once
    assert "Receipt filed" in posted_texts(slack_client)[-1]       # user told it's filed
    slack_client.chat_update.assert_called_once()                  # button still replaced
    assert any(r.levelname == "ERROR" and "sub-1" in r.getMessage()
               for r in caplog.records)


def test_finance_post_transient_failure_succeeds_on_retry(handlers, pending, slack_client,
                                                          monkeypatch):
    post = MagicMock(side_effect=[RuntimeError("ratelimited"), {"ok": True}])
    monkeypatch.setattr(slack_helpers, "post_message", post)
    _, submit = handlers
    _submit(submit, slack_client)
    assert post.call_count == 2
    assert supabase_store.get("sub-1")["status"] == "confirmed"


def test_user_dm_failure_does_not_affect_filing(handlers, pending, slack_client):
    slack_client.chat_postMessage.side_effect = RuntimeError("cannot_dm_user")
    _, submit = handlers
    _submit(submit, slack_client)   # must not raise
    assert supabase_store.get("sub-1")["status"] == "confirmed"
    slack_client.chat_update.assert_called_once()

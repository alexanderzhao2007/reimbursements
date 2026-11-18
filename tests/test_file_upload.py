"""DM upload listener — the intake pipeline wired end to end.

Real modules are used for image prep, hashing and the store (against the
in-memory Supabase fake); only the Slack download and Claude Vision are stubbed.
"""

import io
from unittest.mock import MagicMock

import pytest
from PIL import Image

from app.listeners import file_upload
from models.schemas import ReceiptExtraction
from services import slack_helpers, supabase_store, vision
from services.vision import VisionParseError
from tests.conftest import posted_texts
from utils import image as image_utils


class FakeApp:
    """Captures handlers registered via Bolt-style decorators."""

    def __init__(self):
        self.events, self.actions, self.views = {}, {}, {}

    def _reg(self, store, key):
        def deco(fn):
            store[key] = fn
            return fn
        return deco

    def event(self, name):
        return self._reg(self.events, name)

    def action(self, name):
        return self._reg(self.actions, name)

    def view(self, name):
        return self._reg(self.views, name)


def _jpeg(color=(200, 100, 50)):
    buf = io.BytesIO()
    Image.new("RGB", (120, 80), color).save(buf, format="JPEG")
    return buf.getvalue()


FILE = {"id": "F1", "url_private": "https://files.slack.com/x"}


@pytest.fixture
def pipeline(monkeypatch, fake_db, slack_client):
    """Stub the network edges; return knobs for each test to adjust."""
    knobs = MagicMock()
    knobs.download.return_value = _jpeg()
    knobs.extract.return_value = ReceiptExtraction(vendor="Cafe", total=12.5)
    monkeypatch.setattr(slack_helpers, "download_file", knobs.download)
    monkeypatch.setattr(vision, "extract_receipt", knobs.extract)
    knobs.db, knobs.client = fake_db, slack_client
    return knobs


def _run(p, file_obj=FILE):
    file_upload._process(p.client, "D1", "U1", file_obj)


def _review_button(client):
    for call in client.chat_postMessage.call_args_list:
        for block in call.kwargs.get("blocks") or []:
            if block["type"] == "actions":
                return block["elements"][0]
    return None


# --- _process ---------------------------------------------------------------

def test_happy_path_inserts_pending_and_posts_review_button(pipeline):
    _run(pipeline)
    [row] = pipeline.db.rows()
    assert row["status"] == "pending"
    assert row["user_id"] == "U1" and row["file_id"] == "F1"
    assert row["vendor"] == "Cafe" and row["total"] == 12.5

    button = _review_button(pipeline.client)
    assert button["action_id"] == "open_review_modal"
    assert button["value"] == row["submission_id"]
    summary = pipeline.client.chat_postMessage.call_args.kwargs["blocks"][0]["text"]["text"]
    assert "Cafe" in summary and "USD 12.50" in summary


def test_vision_receives_normalized_png(pipeline):
    _run(pipeline)
    img_bytes, media_type = pipeline.extract.call_args.args
    assert media_type == "image/png"
    assert img_bytes.startswith(b"\x89PNG")


def test_hash_is_of_normalized_bytes(pipeline):
    _run(pipeline)
    img_bytes, _ = pipeline.extract.call_args.args
    assert pipeline.db.rows()[0]["image_hash"] == image_utils.sha256_hash(img_bytes)


def test_download_failure_tells_user_and_stops(pipeline):
    pipeline.download.side_effect = ValueError("html")
    _run(pipeline)
    assert "Couldn't download" in posted_texts(pipeline.client)[-1]
    assert pipeline.db.rows() == []
    pipeline.extract.assert_not_called()


def test_undecodable_file_tells_user_unsupported(pipeline):
    pipeline.download.return_value = b"%PDF-1.4 not an image"
    _run(pipeline)
    assert "Unsupported image format" in posted_texts(pipeline.client)[-1]
    assert pipeline.db.rows() == []


def _reupload(p, user_id="U1"):
    """Upload the same image again (optionally as another member)."""
    p.extract.reset_mock()
    p.client.chat_postMessage.reset_mock()
    file_upload._process(p.client, "D1", user_id, FILE)


def _last_summary(client):
    blocks = client.chat_postMessage.call_args.kwargs.get("blocks")
    return blocks[0]["text"]["text"] if blocks else None


def test_reupload_of_unfinished_submission_reoffers_same_one(pipeline):
    _run(pipeline)
    [row] = pipeline.db.rows()

    _reupload(pipeline)
    assert len(pipeline.db.rows()) == 1           # no second row
    pipeline.extract.assert_not_called()          # no second Vision call
    assert _review_button(pipeline.client)["value"] == row["submission_id"]
    assert "haven't submitted it yet" in _last_summary(pipeline.client)


def test_reoffered_submission_keeps_stored_values(pipeline):
    _run(pipeline)
    _reupload(pipeline)
    # The re-offer is built from the stored row, so the follow-up modal is
    # pre-filled with what was extracted the first time.
    sid = _review_button(pipeline.client)["value"]
    assert supabase_store.get(sid)["vendor"] == "Cafe"


def test_reupload_of_errored_submission_offers_retry(pipeline):
    _run(pipeline)
    [row] = pipeline.db.rows()
    supabase_store.mark_error(row["submission_id"])

    _reupload(pipeline)
    assert _review_button(pipeline.client)["value"] == row["submission_id"]
    assert "didn't go through" in _last_summary(pipeline.client)
    pipeline.extract.assert_not_called()


def test_reupload_of_confirmed_submission_is_rejected(pipeline):
    _run(pipeline)
    [row] = pipeline.db.rows()
    row["status"] = "confirmed"

    _reupload(pipeline)
    assert _review_button(pipeline.client) is None
    assert "already submitted" in posted_texts(pipeline.client)[-1]
    assert row["submission_id"] in posted_texts(pipeline.client)[-1]
    pipeline.extract.assert_not_called()


@pytest.mark.parametrize("status", ["pending", "error"])
def test_other_members_unfinished_submission_is_not_handed_over(pipeline, status):
    _run(pipeline)                                # uploaded by U1
    [row] = pipeline.db.rows()
    row["status"] = status

    _reupload(pipeline, user_id="U2")
    assert _review_button(pipeline.client) is None
    text = posted_texts(pipeline.client)[-1]
    assert "another member" in text
    assert row["submission_id"] not in text       # don't leak their submission ID


def test_other_members_confirmed_submission_is_rejected(pipeline):
    _run(pipeline)
    pipeline.db.rows()[0]["status"] = "confirmed"
    _reupload(pipeline, user_id="U2")
    assert "already submitted" in posted_texts(pipeline.client)[-1]


def test_different_receipts_are_both_accepted(pipeline):
    _run(pipeline)
    pipeline.download.return_value = _jpeg(color=(10, 20, 30))
    _run(pipeline)
    assert len(pipeline.db.rows()) == 2


def _race(pipeline, monkeypatch, *, owner, status):
    """Pre-check misses; a concurrent upload of the same image (by `owner`, in
    `status`) lands just before our insert, which then hits the UNIQUE constraint."""
    real_find = supabase_store.find_by_hash
    monkeypatch.setattr(supabase_store, "find_by_hash", lambda _h: None)  # pre-check misses
    real_insert = supabase_store.insert_pending

    def racing_insert(**kw):
        pipeline.db.tables["reimbursements"] = [{
            "submission_id": "other-sub", "image_hash": kw["image_hash"],
            "user_id": owner, "status": status, "vendor": "Racer",
        }]
        monkeypatch.setattr(supabase_store, "find_by_hash", real_find)
        return real_insert(**kw)

    monkeypatch.setattr(supabase_store, "insert_pending", racing_insert)
    _run(pipeline)


def test_insert_race_with_own_concurrent_upload_reoffers_it(pipeline, monkeypatch):
    # e.g. the member sent the same photo twice in one message.
    _race(pipeline, monkeypatch, owner="U1", status="pending")
    assert len(pipeline.db.rows()) == 1
    assert _review_button(pipeline.client)["value"] == "other-sub"


def test_insert_race_with_confirmed_submission_is_rejected(pipeline, monkeypatch):
    _race(pipeline, monkeypatch, owner="U2", status="confirmed")
    assert "other-sub" in posted_texts(pipeline.client)[-1]
    assert _review_button(pipeline.client) is None


def test_existing_row_vanished_after_conflict(pipeline):
    file_upload._handle_existing(pipeline.client, "D1", "U1", None)
    assert "already submitted" in posted_texts(pipeline.client)[-1]


def test_vision_failure_falls_back_to_manual_entry(pipeline):
    pipeline.extract.side_effect = VisionParseError("nope")
    _run(pipeline)
    [row] = pipeline.db.rows()
    assert row["vendor"] is None and row["total"] is None
    assert _review_button(pipeline.client)["value"] == row["submission_id"]
    summary = pipeline.client.chat_postMessage.call_args.kwargs["blocks"][0]["text"]["text"]
    assert "manually" in summary


def test_store_failure_tells_user_to_retry(pipeline, monkeypatch):
    monkeypatch.setattr(supabase_store, "insert_pending",
                        MagicMock(side_effect=RuntimeError("supabase down")))
    _run(pipeline)
    assert "try again" in posted_texts(pipeline.client)[-1]
    assert _review_button(pipeline.client) is None


def test_summary_when_total_unreadable(pipeline):
    pipeline.extract.return_value = ReceiptExtraction(vendor="Cafe")
    _run(pipeline)
    summary = pipeline.client.chat_postMessage.call_args.kwargs["blocks"][0]["text"]["text"]
    assert "amount unread" in summary


def test_unexpected_vision_exception_falls_back_to_manual_entry(pipeline):
    pipeline.extract.side_effect = RuntimeError("SDK bug / pydantic error / etc.")
    _run(pipeline)   # must not raise
    [row] = pipeline.db.rows()
    assert row["vendor"] is None
    assert _review_button(pipeline.client)["value"] == row["submission_id"]


def test_dedup_check_outage_notifies_user(pipeline, monkeypatch):
    monkeypatch.setattr(supabase_store, "find_by_hash",
                        MagicMock(side_effect=ConnectionError("supabase down")))
    _run(pipeline)   # must not raise
    assert "try again" in posted_texts(pipeline.client)[-1]
    pipeline.extract.assert_not_called()
    assert pipeline.db.rows() == []


def test_any_unexpected_error_is_reported_not_swallowed(pipeline, monkeypatch, caplog):
    # e.g. a bug in image prep's hashing, or posting the review message failing.
    monkeypatch.setattr(file_upload, "_post_review",
                        MagicMock(side_effect=RuntimeError("invalid_blocks")))
    _run(pipeline)   # must not raise out of the thread
    assert "Something went wrong" in posted_texts(pipeline.client)[-1]
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_intake_survives_even_if_user_cannot_be_messaged(pipeline):
    pipeline.client.chat_postMessage.side_effect = RuntimeError("channel_not_found")
    _run(pipeline)   # must not raise


# --- handle_message routing ---------------------------------------------------

@pytest.fixture
def handler(monkeypatch, inline_threads):
    app = FakeApp()
    file_upload.register(app)
    process = MagicMock()
    monkeypatch.setattr(file_upload, "_process", process)
    return app.events["message"], process


def _event(**over):
    ev = {"channel_type": "im", "channel": "D1", "user": "U1",
          "subtype": "file_share", "files": [FILE]}
    ev.update(over)
    return ev


def test_dm_file_share_is_processed(handler, slack_client):
    handle, process = handler
    handle(_event(), slack_client)
    process.assert_called_once_with(slack_client, "D1", "U1", FILE)


def test_each_file_processed_separately(handler, slack_client):
    handle, process = handler
    handle(_event(files=[FILE, {"id": "F2"}]), slack_client)
    assert process.call_count == 2


@pytest.mark.parametrize("override", [
    {"channel_type": "channel"},
    {"subtype": "message_changed"},
    {"subtype": "message_deleted"},
    {"bot_id": "B1"},
])
def test_ignored_events(handler, slack_client, override):
    handle, process = handler
    handle(_event(**override), slack_client)
    process.assert_not_called()
    slack_client.chat_postMessage.assert_not_called()


def test_text_only_dm_gets_help(handler, slack_client):
    handle, process = handler
    handle(_event(subtype=None, files=None, text="hi"), slack_client)
    process.assert_not_called()
    assert "photo of a receipt" in posted_texts(slack_client)[0]

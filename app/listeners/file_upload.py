"""DM file-upload listener: the receipt intake pipeline.

A receipt DM arrives as a `message` event with subtype "file_share". Because the
heavy work (download + Claude Vision, several seconds) can exceed Slack's ~3s
event-ack window, each file is handed to a background thread so the listener
returns immediately and Bolt acks fast. The UNIQUE(image_hash) constraint makes
the pipeline idempotent, so a Slack retry can't create a duplicate submission.

Pipeline: download → sniff/convert format → SHA-256 → dedup → Claude Vision →
insert pending row → post a "Review & Submit" button (the modal opens from that
button click, which is the only way to obtain a trigger_id).
"""

import logging
import threading
from uuid import uuid4

from models.schemas import ReceiptExtraction
from services import slack_helpers, supabase_store, vision
from services.supabase_store import DuplicateSubmission
from services.vision import VisionParseError
from utils import config
from utils import image as image_utils

logger = logging.getLogger(__name__)


def _dm(client, channel, text):
    client.chat_postMessage(channel=channel, text=text)


def _post_review(client, channel, submission_id, extraction, manual, summary=None):
    """Post the review message. `summary` overrides the default intro line."""
    if summary is None and manual:
        summary = (
            ":warning: I couldn't read this receipt automatically. "
            "Tap below to enter the details manually."
        )
    elif summary is None:
        vendor = extraction.vendor or "your receipt"
        if extraction.total is not None:
            amount = f"{extraction.currency or 'USD'} {extraction.total:.2f}"
        else:
            amount = "amount unread"
        summary = f":receipt: Got a receipt from *{vendor}* — {amount}. Review and submit:"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Review & Submit"},
                    "style": "primary",
                    "action_id": "open_review_modal",
                    "value": submission_id,
                }
            ],
        },
    ]
    client.chat_postMessage(channel=channel, blocks=blocks, text="Review your reimbursement")


def _handle_existing(client, channel, user_id, existing):
    """Respond to an upload whose image_hash is already on file.

    - Confirmed → it's genuinely a duplicate submission.
    - Unfinished (pending/error) and uploaded by this member → re-offer the same
      submission's Review & Submit button, pre-filled from the stored row (no
      second Vision call, no orphaned rows). Retrying an 'error' row is allowed.
    - Unfinished but uploaded by someone else → don't hand them another member's
      submission; just say it's already been uploaded.
    """
    if not existing:  # row vanished between the conflict and the re-read
        _dm(client, channel, "This receipt was already submitted.")
        return

    submission_id = existing.get("submission_id")
    status = existing.get("status")

    if status not in supabase_store.CONFIRMABLE_STATUSES:
        _dm(client, channel,
            f"This receipt was already submitted (submission `{submission_id}`).")
        return

    if existing.get("user_id") != user_id:
        _dm(client, channel,
            "This receipt has already been uploaded by another member, so I can't "
            "file it again. Reach out to finance if that looks wrong.")
        return

    if status == "error":
        summary = (":repeat: Your earlier attempt to file this receipt didn't go "
                   "through. Tap below to try again:")
    else:
        summary = (":receipt: You've already uploaded this receipt but haven't "
                   "submitted it yet. Pick up where you left off:")
    _post_review(client, channel, submission_id,
                 supabase_store.extraction_from_row(existing), manual=False,
                 summary=summary)


def _process(client, channel, user_id, file_obj):
    """Thread entry point. Nothing may escape a background thread silently — an
    unhandled error would leave the member staring at 'reading your receipt…'
    forever — so anything unexpected is logged and reported to them."""
    try:
        _run_pipeline(client, channel, user_id, file_obj)
    except Exception:
        logger.exception("receipt intake failed for file %s", file_obj.get("id"))
        try:
            _dm(client, channel,
                "Something went wrong processing your receipt — please try "
                "uploading it again.")
        except Exception:
            logger.exception("could not notify %s of intake failure", user_id)


def _run_pipeline(client, channel, user_id, file_obj):
    _dm(client, channel, ":inbox_tray: Got it — reading your receipt…")

    try:
        raw = slack_helpers.download_file(file_obj, config.SLACK_BOT_TOKEN)
    except Exception as e:
        logger.warning("image download failed: %s", e)
        _dm(client, channel, "Couldn't download your receipt — try re-uploading.")
        return

    try:
        img_bytes, media_type = image_utils.prepare_for_vision(raw)
    except Exception as e:
        logger.warning("image prep failed: %s", e)
        _dm(client, channel,
            "Unsupported image format — please upload JPG, PNG, or WebP.")
        return

    # Hash the final bytes we'll send to Vision (post-conversion), matching the
    # pipeline order in ARCHITECTURE.md.
    image_hash = image_utils.sha256_hash(img_bytes)

    try:
        existing = supabase_store.find_by_hash(image_hash)
    except Exception:
        logger.exception("dedup check failed")
        _dm(client, channel,
            "Couldn't save your receipt right now — please try again shortly.")
        return
    if existing:
        _handle_existing(client, channel, user_id, existing)
        return

    manual = False
    try:
        extraction = vision.extract_receipt(img_bytes, media_type)
    except VisionParseError:
        extraction = ReceiptExtraction()  # blank → manual entry in the modal
        manual = True
    except Exception:
        # Anything else from the SDK/parsing (e.g. a validation error on the
        # structured output). Losing the auto-parse shouldn't block filing.
        logger.exception("unexpected error from Claude Vision")
        extraction = ReceiptExtraction()
        manual = True

    submission_id = str(uuid4())
    try:
        supabase_store.insert_pending(
            submission_id=submission_id,
            user_id=user_id,
            file_id=file_obj.get("id", ""),
            image_hash=image_hash,
            extraction=extraction,
        )
    except DuplicateSubmission as d:
        # Lost a race with a concurrent upload of the same image.
        _handle_existing(client, channel, user_id, d.existing)
        return
    except Exception:
        logger.exception("insert_pending failed")
        _dm(client, channel,
            "Couldn't save your receipt right now — please try again shortly.")
        return

    _post_review(client, channel, submission_id, extraction, manual)


def register(app):
    @app.event("message")
    def handle_message(event, client):
        if event.get("channel_type") != "im":
            return
        # Let file shares through; ignore edits/deletes/joins and the bot's echoes.
        if event.get("subtype") not in (None, "file_share"):
            return
        if event.get("bot_id"):
            return

        channel = event.get("channel")
        files = event.get("files") or []
        if not files:
            _dm(client, channel,
                "Send me a photo of a receipt and I'll file it for reimbursement.")
            return

        user_id = event.get("user")
        for file_obj in files:
            threading.Thread(
                target=_process,
                args=(client, channel, user_id, file_obj),
                daemon=True,
            ).start()

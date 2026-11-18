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


def _post_review(client, channel, submission_id, extraction, manual):
    if manual:
        summary = (
            ":warning: I couldn't read this receipt automatically. "
            "Tap below to enter the details manually."
        )
    else:
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


def _process(client, channel, user_id, file_obj):
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

    existing = supabase_store.check_duplicate(image_hash)
    if existing:
        _dm(client, channel,
            f"This receipt was already submitted (submission `{existing}`).")
        return

    manual = False
    try:
        extraction = vision.extract_receipt(img_bytes, media_type)
    except VisionParseError:
        extraction = ReceiptExtraction()  # blank → manual entry in the modal
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
        _dm(client, channel,
            f"This receipt was already submitted (submission `{d.submission_id}`).")
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

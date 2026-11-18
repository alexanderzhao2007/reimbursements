"""Modal handlers: open the confirmation modal, then handle its submission.

- `open_review_modal` (block action): the "Review & Submit" button click carries a
  trigger_id, so this is where the pre-filled modal is opened.
- `reimbursement_modal` (view submission): validate the edited fields, enrich with
  the member's Slack profile, update the row to confirmed, notify finance, and DM
  the user. Post-ack work runs in a thread so the modal closes instantly.
"""

import logging
import math
import threading

from app.views.modals import build_modal, decode_metadata
from models.schemas import ReceiptExtraction
from services import slack_helpers, supabase_store
from utils import config

logger = logging.getLogger(__name__)


# numeric(12, 2) holds at most 10 integer digits; anything larger would make the
# DB update fail, so reject it up front with a field error instead.
_MAX_AMOUNT = 10**10


def _parse_money(raw, *, allow_zero=True):
    """Parse a user-typed amount → (value, error_message).

    Empty input → (None, None); the caller decides whether the field is required.
    Accepts "$1,234.56"-style input, rounds to cents, and rejects anything that
    isn't a finite, non-negative amount that fits the DB column. (float() alone
    happily accepts "nan", "inf" and "-50".)
    """
    if raw is None or not raw.strip():
        return None, None
    cleaned = raw.replace("$", "").replace(",", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None, "Enter a valid number, e.g. 12.50"
    if not math.isfinite(value):
        return None, "Enter a valid number, e.g. 12.50"
    if value < 0:
        return None, "Amount can't be negative."
    if value == 0 and not allow_zero:
        return None, "Amount must be greater than zero."
    if value >= _MAX_AMOUNT:
        return None, "That amount is too large — check for a typo."
    return round(value, 2), None


def _selected_value(values, block_id, action_id, key):
    return values.get(block_id, {}).get(action_id, {}).get(key)


def _finance_blocks(*, name, email, vendor, date_str, category, currency, total, tax, submission_id):
    lines = [f"*From:* {name}"]
    if email:
        lines.append(f"*Email:* {email}")
    lines.append(f"*Vendor:* {vendor or '—'}")
    lines.append(f"*Date:* {date_str or '—'}")
    lines.append(f"*Category:* {category or '—'}")
    lines.append(f"*Total:* {currency} {total:.2f}")
    if tax is not None:
        lines.append(f"*Tax:* {currency} {tax:.2f}")
    lines.append(f"*Submission:* `{submission_id}`")
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "New reimbursement"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]


def _already_filed_text(submission_id):
    return f"This receipt was already submitted (submission `{submission_id}`)."


_FINANCE_POST_ATTEMPTS = 2


def _notify_finance(client, submission_id, *, text, blocks) -> bool:
    """Post the new-reimbursement notice, retrying once. Never raises: the row is
    already confirmed, so a Slack failure here must not undo the submission. A
    final failure is logged at ERROR with the submission ID so it can be found
    and followed up (the row itself is visible in Supabase either way)."""
    for attempt in range(1, _FINANCE_POST_ATTEMPTS + 1):
        try:
            slack_helpers.post_message(client, config.SLACK_FINANCE_CHANNEL_ID,
                                       text=text, blocks=blocks)
            return True
        except Exception:
            logger.warning("finance notification for %s failed (attempt %d/%d)",
                           submission_id, attempt, _FINANCE_POST_ATTEMPTS,
                           exc_info=True)
    logger.error("FINANCE NOT NOTIFIED for confirmed submission %s", submission_id)
    return False


def _safe_dm(client, user_id, text):
    """DM the member; a failure is logged, never raised."""
    try:
        client.chat_postMessage(channel=user_id, text=text)
    except Exception:
        logger.warning("could not DM %s", user_id, exc_info=True)


def _mark_review_message_filed(client, channel, ts, submission_id):
    """Replace the review message's button with a 'Submitted' note (Slack has no
    way to disable a button). Best-effort: the DB status guard is what actually
    prevents a resubmit, so a failure here is logged, never surfaced."""
    if not (channel and ts):
        return
    text = f":white_check_mark: Submitted — `{submission_id}`"
    try:
        client.chat_update(
            channel=channel, ts=ts, text=text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )
    except Exception:
        logger.warning("could not replace review button for %s", submission_id,
                       exc_info=True)


def register(app):
    @app.action("open_review_modal")
    def open_review_modal(ack, body, client):
        ack()
        submission_id = body["actions"][0]["value"]
        user_id = body["user"]["id"]
        channel = (body.get("channel") or {}).get("id")
        ts = (body.get("message") or {}).get("ts")

        row = supabase_store.get(submission_id)
        if row is None:
            client.chat_postMessage(
                channel=user_id,
                text="I couldn't find that submission — please re-upload the receipt.",
            )
            return
        if row.get("status") not in supabase_store.CONFIRMABLE_STATUSES:
            # Stale button on an already-filed receipt: say so and remove it.
            _mark_review_message_filed(client, channel, ts, submission_id)
            client.chat_postMessage(channel=user_id,
                                    text=_already_filed_text(submission_id))
            return

        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_modal(submission_id, supabase_store.extraction_from_row(row),
                             channel=channel, ts=ts),
        )

    @app.view("reimbursement_modal")
    def handle_submit(ack, body, view, client):
        values = view["state"]["values"]
        vendor = _selected_value(values, "vendor", "vendor", "value")
        date_str = _selected_value(values, "date", "date", "selected_date")
        total_raw = _selected_value(values, "total", "total", "value")
        currency = _selected_value(values, "currency", "currency", "value") or "USD"
        cat_opt = _selected_value(values, "category", "category", "selected_option")
        category = cat_opt["value"] if cat_opt else None
        tax_raw = _selected_value(values, "tax", "tax", "value")

        # Validate money fields; total is required, tax optional.
        errors = {}
        total_val, total_err = _parse_money(total_raw, allow_zero=False)
        if total_err:
            errors["total"] = total_err
        elif total_val is None:
            errors["total"] = "Enter the total amount."
        tax_val, tax_err = _parse_money(tax_raw)
        if tax_err:
            errors["tax"] = tax_err
        elif tax_val is not None and total_val is not None and tax_val > total_val:
            errors["tax"] = "Tax can't be more than the total."

        if errors:
            ack(response_action="errors", errors=errors)
            return

        ack()  # close the modal immediately

        submission_id, review_channel, review_ts = decode_metadata(view["private_metadata"])
        user_id = body["user"]["id"]
        row = supabase_store.get(submission_id)
        line_items = row.get("line_items") if row else None

        extraction = ReceiptExtraction(
            vendor=vendor,
            date=date_str,
            total=total_val,
            currency=currency,
            category=category,
            tax=tax_val,
            line_items=line_items,
        )

        def _finalize():
            # Phase 1 — persist. This is the only step whose failure means the
            # receipt is NOT filed, so it's the only one that marks the row 'error'
            # (which leaves it retryable via the same Review & Submit button).
            try:
                profile = slack_helpers.get_user_profile(client, user_id)
                confirmed = supabase_store.confirm(
                    submission_id=submission_id,
                    extraction=extraction,
                    employee_name=profile["employee_name"],
                    employee_email=profile["employee_email"],
                )
            except Exception:
                logger.exception("confirming submission %s failed", submission_id)
                try:
                    supabase_store.mark_error(submission_id)
                except Exception:
                    logger.exception("mark_error failed for %s", submission_id)
                _safe_dm(client, user_id,
                         "Something went wrong filing your receipt. Tap *Review & "
                         "Submit* again to retry, or contact finance and mention "
                         f"submission `{submission_id}`.")
                return

            if confirmed is None:
                # Already confirmed (double submit / stale modal). The atomic
                # status guard rejected the update — no side effects.
                _mark_review_message_filed(client, review_channel, review_ts,
                                           submission_id)
                _safe_dm(client, user_id, _already_filed_text(submission_id))
                return

            # Phase 2 — side effects. The receipt is filed; nothing below may
            # undo that. Each step fails independently and is only logged.
            name = profile["employee_name"] or f"<@{user_id}>"
            _notify_finance(
                client, submission_id,
                text=f"New reimbursement from {name}: {currency} {total_val:.2f} at {vendor or '—'}",
                blocks=_finance_blocks(
                    name=name,
                    email=profile["employee_email"],
                    vendor=vendor,
                    date_str=date_str,
                    category=category,
                    currency=currency,
                    total=total_val,
                    tax=tax_val,
                    submission_id=submission_id,
                ),
            )
            _safe_dm(client, user_id,
                     f":white_check_mark: Receipt filed — submission `{submission_id}`.")
            _mark_review_message_filed(client, review_channel, review_ts, submission_id)

        threading.Thread(target=_finalize, daemon=True).start()

"""Modal handlers: open the confirmation modal, then handle its submission.

- `open_review_modal` (block action): the "Review & Submit" button click carries a
  trigger_id, so this is where the pre-filled modal is opened.
- `reimbursement_modal` (view submission): validate the edited fields, enrich with
  the member's Slack profile, update the row to confirmed, notify finance, and DM
  the user. Post-ack work runs in a thread so the modal closes instantly.
"""

import logging
import threading

from app.views.modals import build_modal
from models.schemas import ReceiptExtraction
from services import slack_helpers, supabase_store
from utils import config

logger = logging.getLogger(__name__)


def _parse_money(raw):
    """Return (value, had_error). Empty input → (None, False)."""
    if raw is None or not raw.strip():
        return None, False
    cleaned = raw.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned), False
    except ValueError:
        return None, True


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


def register(app):
    @app.action("open_review_modal")
    def open_review_modal(ack, body, client):
        ack()
        submission_id = body["actions"][0]["value"]
        row = supabase_store.get(submission_id)
        extraction = (
            supabase_store.extraction_from_row(row) if row else ReceiptExtraction()
        )
        client.views_open(
            trigger_id=body["trigger_id"],
            view=build_modal(submission_id, extraction),
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
        total_val, total_err = _parse_money(total_raw)
        if total_raw is None or not total_raw.strip():
            errors["total"] = "Enter the total amount."
        elif total_err:
            errors["total"] = "Enter a valid number, e.g. 12.50"
        tax_val, tax_err = _parse_money(tax_raw)
        if tax_err:
            errors["tax"] = "Enter a valid number or leave blank."

        if errors:
            ack(response_action="errors", errors=errors)
            return

        ack()  # close the modal immediately

        submission_id = view["private_metadata"]
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
            try:
                profile = slack_helpers.get_user_profile(client, user_id)
                supabase_store.confirm(
                    submission_id=submission_id,
                    extraction=extraction,
                    employee_name=profile["employee_name"],
                    employee_email=profile["employee_email"],
                )
                name = profile["employee_name"] or f"<@{user_id}>"
                slack_helpers.post_message(
                    client,
                    config.SLACK_FINANCE_CHANNEL_ID,
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
                client.chat_postMessage(
                    channel=user_id,
                    text=f":white_check_mark: Receipt filed — submission `{submission_id}`.",
                )
            except Exception:
                logger.exception("finalizing submission %s failed", submission_id)
                try:
                    supabase_store.mark_error(submission_id)
                except Exception:
                    logger.exception("mark_error failed for %s", submission_id)
                client.chat_postMessage(
                    channel=user_id,
                    text=("Something went wrong filing your receipt. "
                          "Please contact finance and mention "
                          f"submission `{submission_id}`."),
                )

        threading.Thread(target=_finalize, daemon=True).start()

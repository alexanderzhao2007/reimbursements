"""Block Kit builder for the receipt confirmation modal.

`build_modal` renders a single-step editable modal pre-filled from a
ReceiptExtraction; `submission_id` rides along in `private_metadata` so the
submit handler can find the pending row. Line items are shown read-only (editing
a variable-length list in a modal is clunky); they carry through unchanged on
confirm.
"""

from models.schemas import ReceiptExtraction

CALLBACK_ID = "reimbursement_modal"
CATEGORIES = ["meals", "travel", "supplies", "software", "equipment", "lodging", "other"]


def _plain_text_input(block_id, label, initial=None, optional=False, placeholder=None):
    element = {"type": "plain_text_input", "action_id": block_id}
    if initial not in (None, ""):
        element["initial_value"] = str(initial)
    if placeholder:
        element["placeholder"] = {"type": "plain_text", "text": placeholder}
    return {
        "type": "input",
        "block_id": block_id,
        "optional": optional,
        "label": {"type": "plain_text", "text": label},
        "element": element,
    }


def _category_option(value):
    return {"text": {"type": "plain_text", "text": value.capitalize()}, "value": value}


def build_modal(submission_id: str, extraction: ReceiptExtraction) -> dict:
    # Date picker
    date_element = {"type": "datepicker", "action_id": "date"}
    if extraction.date:
        date_element["initial_date"] = extraction.date.isoformat()
    date_block = {
        "type": "input",
        "block_id": "date",
        "optional": False,
        "label": {"type": "plain_text", "text": "Date"},
        "element": date_element,
    }

    # Category select
    category_element = {
        "type": "static_select",
        "action_id": "category",
        "options": [_category_option(c) for c in CATEGORIES],
    }
    if extraction.category in CATEGORIES:
        category_element["initial_option"] = _category_option(extraction.category)
    category_block = {
        "type": "input",
        "block_id": "category",
        "optional": False,
        "label": {"type": "plain_text", "text": "Category"},
        "element": category_element,
    }

    # Read-only line-item summary
    if extraction.line_items:
        items_text = "\n".join(
            f"• {li.description} — {li.amount} ×{li.quantity}"
            for li in extraction.line_items
        )
    else:
        items_text = "_No line items extracted._"

    blocks = [
        _plain_text_input("vendor", "Vendor", extraction.vendor,
                          placeholder="e.g. Circle K"),
        date_block,
        _plain_text_input("total", "Total", extraction.total, placeholder="e.g. 42.50"),
        _plain_text_input("currency", "Currency", extraction.currency or "USD"),
        category_block,
        _plain_text_input("tax", "Tax", extraction.tax, optional=True,
                          placeholder="optional"),
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Line items* (from the receipt, not editable here):\n{items_text}",
            },
        },
    ]

    return {
        "type": "modal",
        "callback_id": CALLBACK_ID,
        "private_metadata": submission_id,
        "title": {"type": "plain_text", "text": "Reimbursement"},
        "submit": {"type": "plain_text", "text": "Submit Reimbursement"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }

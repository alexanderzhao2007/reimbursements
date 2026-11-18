"""Supabase (Postgres) data access for the reimbursements table.

Covers the operations the pipeline needs: insert a pending row, dedup lookup,
confirm (update to status='confirmed'), and per-user listing. Duplicate detection
is enforced by the UNIQUE(image_hash) constraint — a conflicting insert raises
DuplicateSubmission carrying the existing submission_id.

Field-name mapping: the Pydantic Submission model calls it `employee_email`, but
the Postgres column is `email`; `confirm()` maps between them.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from postgrest.exceptions import APIError
from supabase import Client, create_client

from models.schemas import ReceiptExtraction
from utils import config

logger = logging.getLogger(__name__)

_TABLE = "reimbursements"
_UNIQUE_VIOLATION = "23505"  # Postgres error code for unique_violation

# Statuses a submission may be confirmed from. 'error' is included so a member can
# retry via the same Review & Submit button after a transient failure.
CONFIRMABLE_STATUSES = ("pending", "error")

_client: Optional[Client] = None


class DuplicateSubmission(Exception):
    """Raised when an insert conflicts with an existing image_hash. Carries the
    row already on file (`existing`, may be None if it can't be re-read) so the
    caller can decide by its status/owner how to respond."""

    def __init__(self, existing: Optional[dict]):
        self.existing = existing
        self.submission_id = existing.get("submission_id") if existing else None
        super().__init__(f"duplicate receipt; existing submission {self.submission_id}")


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(
            config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY
        )
    return _client


def _extraction_columns(extraction: ReceiptExtraction) -> dict:
    """Flatten a ReceiptExtraction into JSON-safe column values (date -> ISO
    string, line_items -> list of dicts) for insert/update."""
    data = extraction.model_dump(mode="json")
    return {
        "vendor": data["vendor"],
        "date": data["date"],
        "total": data["total"],
        "currency": data["currency"],
        "category": data["category"],
        "tax": data["tax"],
        "line_items": data["line_items"],
    }


def get(submission_id: str) -> Optional[dict]:
    """Fetch a single reimbursement row by submission_id, or None."""
    res = (
        _get_client()
        .table(_TABLE)
        .select("*")
        .eq("submission_id", submission_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def extraction_from_row(row: dict) -> ReceiptExtraction:
    """Rebuild a ReceiptExtraction from a stored row (inverse of
    _extraction_columns), e.g. to pre-fill the confirmation modal."""
    return ReceiptExtraction(
        vendor=row.get("vendor"),
        date=row.get("date"),
        total=row.get("total"),
        currency=row.get("currency"),
        category=row.get("category"),
        tax=row.get("tax"),
        line_items=row.get("line_items"),
    )


def find_by_hash(image_hash: str) -> Optional[dict]:
    """Return the existing row with this image_hash, or None. The full row is
    returned so the caller can branch on status/user_id and, for an unfinished
    submission, re-offer it pre-filled without another Vision call."""
    res = (
        _get_client()
        .table(_TABLE)
        .select("*")
        .eq("image_hash", image_hash)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def insert_pending(
    *,
    submission_id: str,
    user_id: str,
    file_id: str,
    image_hash: str,
    extraction: ReceiptExtraction,
) -> None:
    """Insert a pending row with the extracted fields pre-filled.

    Raises DuplicateSubmission if image_hash already exists (relies on the
    UNIQUE constraint rather than a check-then-insert race).
    """
    row = {
        "submission_id": submission_id,
        "user_id": user_id,
        "file_id": file_id,
        "image_hash": image_hash,
        "status": "pending",
        **_extraction_columns(extraction),
    }
    try:
        _get_client().table(_TABLE).insert(row).execute()
    except APIError as e:
        if e.code == _UNIQUE_VIOLATION:
            raise DuplicateSubmission(find_by_hash(image_hash)) from e
        raise


def confirm(
    *,
    submission_id: str,
    extraction: ReceiptExtraction,
    employee_name: Optional[str],
    employee_email: Optional[str],
) -> Optional[dict]:
    """Update a pending (or previously errored) row to confirmed: writes the
    (possibly user-edited) extraction fields, member info, and submitted_at.

    The status guard is part of the UPDATE's WHERE clause, so the check-and-set is
    atomic in Postgres: if two submits race (double click, two open modals), only
    the first matches and the second gets None. Returns the updated row, or None if
    the submission doesn't exist or is already confirmed — callers must then skip
    any side effects (finance notification, etc.)."""
    updates = {
        "status": "confirmed",
        "employee_name": employee_name,
        "email": employee_email,           # model.employee_email -> column `email`
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        **_extraction_columns(extraction),
    }
    res = (
        _get_client()
        .table(_TABLE)
        .update(updates)
        .eq("submission_id", submission_id)
        .in_("status", list(CONFIRMABLE_STATUSES))
        .execute()
    )
    return res.data[0] if res.data else None


def mark_error(submission_id: str) -> None:
    """Mark a row as errored (used when downstream processing fails)."""
    _get_client().table(_TABLE).update({"status": "error"}).eq(
        "submission_id", submission_id
    ).execute()


def list_by_user(user_id: str, limit: int = 50) -> list[dict]:
    """Return a user's submissions, newest first."""
    res = (
        _get_client()
        .table(_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data

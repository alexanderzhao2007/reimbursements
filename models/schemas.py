"""Pydantic v2 models for the reimbursement pipeline.

`ReceiptExtraction` mirrors the JSON schema Claude Vision is prompted to return
(every field nullable — Claude returns null rather than guessing). `Submission`
maps 1:1 to a row in the Supabase `reimbursements` table.
"""

from datetime import date as _date  # aliased: the field named `date` below would
                                     # otherwise shadow this type during Pydantic
                                     # hint resolution, collapsing it to Optional[None]
from typing import Optional

from pydantic import BaseModel


class LineItem(BaseModel):
    description: str
    amount: float
    quantity: Optional[int] = 1


class ReceiptExtraction(BaseModel):
    """Schema for Claude Vision response. All fields nullable — Claude must
    return null for anything it cannot read, never guess."""

    vendor: Optional[str] = None
    date: Optional[_date] = None
    total: Optional[float] = None
    currency: Optional[str] = "USD"
    category: Optional[str] = None
    tax: Optional[float] = None
    line_items: Optional[list[LineItem]] = None


class Submission(BaseModel):
    """Full submission record. Maps 1:1 to a row in the Supabase
    `reimbursements` table (see migrations/001_reimbursements.sql)."""

    submission_id: str          # uuid4
    user_id: str                # Slack user ID — kept for per-person querying
    file_id: str                # Slack file ID
    image_hash: str             # SHA-256 hex digest (UNIQUE in Postgres)
    extraction: ReceiptExtraction
    employee_name: Optional[str] = None
    employee_email: Optional[str] = None
    department: Optional[str] = None
    status: str = "pending"     # pending | confirmed | error
    created_at: str             # ISO 8601 timestamp

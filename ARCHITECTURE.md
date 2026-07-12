# DataStory Slack Reimbursement Bot — Revised Architecture

## Overview

Members upload receipt photos to a designated Slack channel. The bot extracts
structured data via Claude Vision, validates it with Pydantic, deduplicates via
SHA-256 hashing, presents an editable confirmation modal, then logs the
submission to Google Sheets and notifies the finance team in a Slack channel.

---

## Tech Stack

- **Python 3.11+**
- **Slack Bolt** (`slack-bolt`) — event handling, modals, message composition
- **Anthropic Python SDK** — Claude Vision for receipt parsing (model: `claude-sonnet-4-6`)
- **Redis** (`redis-py`) — submission state persistence, duplicate detection, TTL-based cleanup
- **Pydantic v2** — schema validation for Claude Vision responses
- **Google Sheets API** (`google-api-python-client`) — logging confirmed submissions
- **Pillow** — image format detection; fallback conversion for unsupported types
- **Hosting**: Railway (recommended) or Render paid tier (free tier spins down and causes Slack timeouts)

---

## Flow Diagram

```
Member uploads receipt image to #reimbursements
                    │
                    ▼
         ┌────────────────────┐
         │   Slack Bolt App   │
         │   ack() < 3 sec    │──── immediately return 200 to Slack
         │   (lazy listener)  │      process everything async
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Download Image    │
         │  via Slack API     │──── GET file URL with Authorization: Bearer <bot_token>
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Check Filetype    │──── read from Slack file object `filetype` field
         │  (Pillow verify)   │
         └─────────┬──────────┘
                   │
            ┌──────┴──────┐
            │             │
     supported        unsupported
   (jpg, png,       (heic, tiff,
    gif, webp)       bmp, etc.)
            │             │
            │             ▼
            │      ┌──────────────┐
            │      │ Convert via  │
            │      │ Pillow → PNG │
            │      └──────┬───────┘
            │             │
            └──────┬──────┘
                   │
                   ▼
         ┌────────────────────┐
         │  SHA-256 Hash      │──── hash raw image bytes
         │  of image bytes    │
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Redis: Duplicate  │──── key: dup:<sha256>  value: submission_id
         │  Check             │     if exists → notify user "already submitted"
         └─────────┬──────────┘     and stop
                   │ (no duplicate)
                   ▼
         ┌────────────────────┐
         │  Claude Vision     │──── base64 encode image
         │  (claude-sonnet-4-6) │    structured JSON prompt
         │                    │     explicit: return null for unreadable fields
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Pydantic Schema   │──── validate JSON shape, strip markdown fences
         │  Validation        │     if parse fails → open blank modal for manual entry
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Generate          │──── uuid4-based submission ID
         │  Submission ID     │
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Store in Redis    │──── key: sub:<submission_id>
         │                    │     value: JSON blob of extracted data + user_id + file_id
         │                    │     TTL: 24 hours (auto-cleanup of orphaned submissions)
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Open Slack Modal  │──── single-step editable modal
         │  (Block Kit)       │     all extracted fields pre-filled
         │                    │     null fields shown as empty inputs for manual entry
         │                    │     user edits anything wrong + submits in one action
         └─────────┬──────────┘
                   │ user clicks "Submit Reimbursement"
                   ▼
         ┌────────────────────┐
         │  Pull Slack        │──── users.info API → real_name, email, profile.department
         │  Profile Data      │     (requires users:read + users:read.email scopes)
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Append Row to     │──── columns: submission_id, user_id, employee_name,
         │  Google Sheets     │     email, department, vendor, date, total, category,
         │                    │     line_items (JSON), image_hash, timestamp
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Notify Finance    │──── post summary message to #finance-reimbursements
         │  (Slack message)   │     includes: who, amount, vendor, link to sheet row
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Confirm to User   │──── ephemeral message in original channel
         │                    │     "Receipt filed — submission <id>"
         └────────────────────┘
```

---

## Data Models (Pydantic)

```python
from pydantic import BaseModel
from typing import Optional
from datetime import date

class LineItem(BaseModel):
    description: str
    amount: float
    quantity: Optional[int] = 1

class ReceiptExtraction(BaseModel):
    """Schema for Claude Vision response. All fields nullable — Claude must
    return null for anything it cannot read, never guess."""
    vendor: Optional[str] = None
    date: Optional[date] = None
    total: Optional[float] = None
    currency: Optional[str] = "USD"
    category: Optional[str] = None
    tax: Optional[float] = None
    line_items: Optional[list[LineItem]] = None

class Submission(BaseModel):
    """Full submission record stored in Redis and eventually written to Sheets."""
    submission_id: str          # uuid4
    user_id: str                # Slack user ID — kept for per-person querying
    file_id: str                # Slack file ID
    image_hash: str             # SHA-256 hex digest
    extraction: ReceiptExtraction
    employee_name: Optional[str] = None
    employee_email: Optional[str] = None
    department: Optional[str] = None
    status: str = "pending"     # pending | confirmed | error
    created_at: str             # ISO 8601 timestamp
```

---

## Redis Key Schema

| Key Pattern | Value | TTL | Purpose |
|---|---|---|---|
| `sub:<submission_id>` | JSON-serialized `Submission` | 24 hours | Pending submission state between upload and confirmation |
| `dup:<sha256_hex>` | `<submission_id>` | 30 days | Duplicate image detection |
| `user:<user_id>:subs` | Redis SET of `submission_id`s | none | Per-user submission index for querying |

---

## Claude Vision Prompt

```
You are a receipt parser. Analyze this receipt image and extract structured data.

Return ONLY valid JSON matching this exact schema — no markdown fences, no
explanation, no preamble:

{
  "vendor": string or null,
  "date": "YYYY-MM-DD" or null,
  "total": number or null,
  "currency": "USD" (3-letter ISO code) or null,
  "category": string or null,
  "tax": number or null,
  "line_items": [{"description": string, "amount": number, "quantity": integer}] or null
}

Rules:
- Return null for ANY field you cannot read clearly. Never guess.
- "total" is the final amount paid including tax and tip.
- "category" should be one of: meals, travel, supplies, software, equipment, lodging, other.
- "date" must be ISO 8601 format (YYYY-MM-DD).
- If the receipt is not in English, still extract the data and translate the vendor name.
```

---

## Slack Configuration

### Required OAuth Scopes
- `files:read` — download uploaded receipt images
- `chat:write` — send confirmation and finance notifications
- `im:history` — receive DM events (if supporting DM uploads)
- `channels:history` — receive channel message events
- `users:read` — pull employee name and department
- `users:read.email` — pull employee email for Sheets logging

### Event Subscriptions
- `file_shared` — triggers the receipt processing pipeline

### Interactivity
- Enable interactivity for modal submissions
- Request URL points to your server's `/slack/events` endpoint

---

## Google Sheets Schema

| Column | Source | Type |
|---|---|---|
| A: submission_id | Generated UUID | string |
| B: user_id | Slack event | string |
| C: employee_name | Slack users.info | string |
| D: email | Slack users.info | string |
| E: department | Slack users.info | string |
| F: vendor | Claude Vision / user edit | string |
| G: date | Claude Vision / user edit | date |
| H: total | Claude Vision / user edit | number |
| I: currency | Claude Vision / user edit | string |
| J: category | Claude Vision / user edit | string |
| K: tax | Claude Vision / user edit | number |
| L: line_items | Claude Vision / user edit | JSON string |
| M: image_hash | SHA-256 of file bytes | string |
| N: submitted_at | Server timestamp | ISO 8601 |

---

## Project Structure

```
datastory-bot/
├── app/
│   ├── __init__.py
│   ├── main.py              # Slack Bolt app initialization, event/action routing
│   ├── listeners/
│   │   ├── __init__.py
│   │   ├── file_upload.py   # file_shared event → download, hash, extract, open modal
│   │   └── modal_submit.py  # modal submission → validate, enrich, log, notify
│   └── views/
│       ├── __init__.py
│       └── modals.py        # Block Kit modal builder for receipt confirmation
├── models/
│   ├── __init__.py
│   └── schemas.py           # Pydantic models: LineItem, ReceiptExtraction, Submission
├── services/
│   ├── __init__.py
│   ├── vision.py            # Claude Vision API call + response parsing
│   ├── redis_store.py       # Redis client: store/retrieve/check-duplicate submissions
│   ├── sheets.py            # Google Sheets append + service account auth
│   └── slack_helpers.py     # Image download, profile lookup, message posting
├── utils/
│   ├── __init__.py
│   ├── image.py             # Filetype check, Pillow conversion, base64 encoding, SHA-256
│   └── config.py            # Environment variable loading (tokens, Redis URL, sheet ID)
├── tests/
│   ├── __init__.py
│   ├── test_schemas.py      # Pydantic model validation tests
│   ├── test_vision.py       # Mock Claude responses, edge cases, malformed JSON
│   ├── test_image.py        # Filetype detection, hash consistency
│   └── test_redis_store.py  # Store/retrieve/duplicate detection
├── .env.example             # Template for required env vars
├── requirements.txt
├── Procfile                 # Railway/Render entry point
└── README.md
```

---

## Environment Variables

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_FINANCE_CHANNEL_ID=C...       # #finance-reimbursements channel ID
ANTHROPIC_API_KEY=sk-ant-...
REDIS_URL=redis://...                # Railway provides this automatically
GOOGLE_SHEETS_ID=...                 # ID from the sheet URL
GOOGLE_SERVICE_ACCOUNT_JSON=...      # base64-encoded service account key JSON
```

---

## Error Handling Strategy

| Failure Point | Behavior |
|---|---|
| Image download fails | Notify user: "Couldn't download your receipt — try re-uploading" |
| Unsupported filetype + conversion fails | Notify user: "Unsupported image format — please upload JPG, PNG, or WebP" |
| Duplicate detected | Notify user: "This receipt was already submitted (submission \<id\>)" |
| Claude returns invalid JSON | Strip markdown fences, retry parse; if still fails → open blank modal for full manual entry |
| Claude API error / timeout | Notify user: "Couldn't read your receipt automatically — opening manual entry"; open blank modal |
| Pydantic validation fails | Log the raw response for debugging; open blank modal for manual entry |
| Redis unavailable | Fall back to in-memory dict with warning log; process continues but no duplicate detection |
| Google Sheets API error | Retry once; if still fails → store in Redis with status "error", notify user to contact finance |
| Slack profile lookup fails | Use Slack display name as fallback; leave email/department as "unknown" |

---

## Implementation Order

1. `utils/config.py` + `.env.example` — env var loading
2. `models/schemas.py` — Pydantic models
3. `app/main.py` — bare Slack Bolt server that acks `file_shared` events
4. `services/slack_helpers.py` — image download + profile lookup
5. `utils/image.py` — filetype check, conversion, hashing, base64 encoding
6. `services/redis_store.py` — store, retrieve, duplicate check
7. `services/vision.py` — Claude Vision call + Pydantic parsing
8. `app/listeners/file_upload.py` — full upload pipeline wired together
9. `app/views/modals.py` — Block Kit modal builder
10. `app/listeners/modal_submit.py` — confirmation handler
11. `services/sheets.py` — Google Sheets append
12. Finance notification message to Slack channel
13. Tests

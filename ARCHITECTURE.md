# DataStory Slack Reimbursement Bot — Revised Architecture

## Overview

Members upload receipt photos to a designated Slack channel. The bot extracts
structured data via Claude Vision, validates it with Pydantic, deduplicates via
SHA-256 hashing, presents an editable confirmation modal, then writes the
submission to Supabase (Postgres) and notifies the finance team in a Slack channel.

---

## Credentials to Obtain

Only two of these are true API keys; the rest are tokens or host-provided
connection strings.

| Service | Credential(s) | Where to obtain | Manual? |
|---|---|---|---|
| **Anthropic** (Claude Vision) | `ANTHROPIC_API_KEY` (`sk-ant-…`) | console.anthropic.com → API Keys | Yes — set a spend limit; billed per receipt parsed |
| **Slack** | `SLACK_BOT_TOKEN` (`xoxb-…`), `SLACK_SIGNING_SECRET` | api.slack.com/apps → *OAuth & Permissions* (token) and *Basic Information* (signing secret) | Yes — bot token issued when the app is installed with the scopes below |
| **Supabase** | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | supabase.com → project → *Settings → API* | Yes — service-role key for server-side writes |

The Supabase migration removes the previous Google credentials
(`GOOGLE_SHEETS_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`) and the entire GCP project /
service-account / sheet-sharing setup.

---

## Tech Stack

- **Python 3.11+**
- **Slack Bolt** (`slack-bolt`) — event handling, modals, message composition
- **Anthropic Python SDK** — Claude Vision for receipt parsing (model: `claude-sonnet-5`)
- **Supabase** (`supabase-py`) — Postgres storage for confirmed submissions,
  pending-submission state, and duplicate detection (native `UNIQUE` constraint)
- **Pydantic v2** — schema validation for Claude Vision responses
- **Pillow** — image format detection; fallback conversion for unsupported types
- **Hosting**: Railway (recommended) or Render paid tier (free tier spins down and causes Slack timeouts)

> **Storage decision:** Supabase (Postgres) handles everything the design previously
> split between Redis and Google Sheets. Dedup is a `UNIQUE(image_hash)` constraint
> (permanent and stronger than a TTL key); pending state is a row with
> `status = 'pending'`; per-user querying is plain SQL. This is a low-volume finance
> workflow, so Postgres latency is irrelevant and the ops surface collapses to two
> external services (Anthropic + Supabase). Redis would only be worth reintroducing
> for high write volume or strict TTL auto-expiry semantics — neither applies here.

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
         │  Supabase: Dedup   │──── SELECT id FROM reimbursements WHERE image_hash = <sha256>
         │  Check             │     if a row exists → notify user "already submitted"
         └─────────┬──────────┘     and stop
                   │ (no duplicate)
                   ▼
         ┌────────────────────┐
         │  Claude Vision     │──── base64 encode image
         │  (claude-sonnet-5) │     structured JSON prompt
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
         │  Insert Pending    │──── INSERT INTO reimbursements (...) with status='pending'
         │  Row in Supabase   │     ON CONFLICT (image_hash) → treat as duplicate
         │                    │     submission_id also carried in modal private_metadata
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
         │  Update Row in     │──── UPDATE reimbursements SET status='confirmed',
         │  Supabase          │     vendor, date, total, category, line_items (jsonb),
         │                    │     employee_name, email, department, ... WHERE submission_id=...
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Notify Finance    │──── post summary message to #finance-reimbursements
         │  (Slack message)   │     includes: who, amount, vendor, link to Supabase row
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
    """Full submission record. Maps 1:1 to a row in the Supabase
    `reimbursements` table (see schema below)."""
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
```

---

## Supabase Schema

A single `reimbursements` table serves confirmed records, pending state, and dedup.
The `UNIQUE (image_hash)` constraint enforces duplicate detection natively — no
separate key or TTL is needed. Pending submissions are just rows with
`status = 'pending'`; the upload→modal handoff also carries `submission_id` in the
Slack modal `private_metadata`, so the row is the durable backstop rather than a
hot-path lookup.

```sql
create table reimbursements (
    submission_id   uuid        primary key default gen_random_uuid(),
    user_id         text        not null,
    file_id         text,
    employee_name   text,
    email           text,
    department      text,
    vendor          text,
    date            date,
    total           numeric(12, 2),
    currency        text        default 'USD',
    category        text,
    tax             numeric(12, 2),
    line_items      jsonb,
    image_hash      text        not null,
    status          text        not null default 'pending'
                                check (status in ('pending', 'confirmed', 'error')),
    created_at      timestamptz not null default now(),
    submitted_at    timestamptz,
    constraint uq_reimbursements_image_hash unique (image_hash)
);

create index idx_reimbursements_user_id    on reimbursements (user_id);
create index idx_reimbursements_created_at on reimbursements (created_at);
create index idx_reimbursements_status     on reimbursements (status);
```

Optional cleanup of orphaned pending rows (never confirmed) can be handled by a
`pg_cron` job — e.g. delete `status = 'pending'` rows older than 24 hours — or simply
by filtering on `status` in queries. It is not required for correctness.

| Column | Source | Postgres Type |
|---|---|---|
| submission_id | Generated UUID | `uuid` (PK) |
| user_id | Slack event | `text` |
| employee_name | Slack users.info | `text` |
| email | Slack users.info | `text` |
| department | Slack users.info | `text` |
| vendor | Claude Vision / user edit | `text` |
| date | Claude Vision / user edit | `date` |
| total | Claude Vision / user edit | `numeric(12,2)` |
| currency | Claude Vision / user edit | `text` |
| category | Claude Vision / user edit | `text` |
| tax | Claude Vision / user edit | `numeric(12,2)` |
| line_items | Claude Vision / user edit | `jsonb` |
| image_hash | SHA-256 of file bytes | `text` (UNIQUE) |
| status | Server | `text` |
| created_at | Server default | `timestamptz` |
| submitted_at | Set on confirmation | `timestamptz` |

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
- `users:read.email` — pull employee email for the record

### Event Subscriptions
- `file_shared` — triggers the receipt processing pipeline

### Interactivity
- Enable interactivity for modal submissions
- Request URL points to your server's `/slack/events` endpoint

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
│   ├── supabase_store.py    # Supabase client: insert/update/dedup/per-user queries
│   └── slack_helpers.py     # Image download, profile lookup, message posting
├── utils/
│   ├── __init__.py
│   ├── image.py             # Filetype check, Pillow conversion, base64 encoding, SHA-256
│   └── config.py            # Environment variable loading (tokens, Supabase URL/key)
├── migrations/
│   └── 001_reimbursements.sql  # reimbursements table DDL (see Supabase Schema)
├── tests/
│   ├── __init__.py
│   ├── test_schemas.py         # Pydantic model validation tests
│   ├── test_vision.py          # Mock Claude responses, edge cases, malformed JSON
│   ├── test_image.py           # Filetype detection, hash consistency
│   └── test_supabase_store.py  # Insert/update/duplicate detection
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
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...       # service-role key for server-side writes
```

---

## Error Handling Strategy

| Failure Point | Behavior |
|---|---|
| Image download fails | Notify user: "Couldn't download your receipt — try re-uploading" |
| Unsupported filetype + conversion fails | Notify user: "Unsupported image format — please upload JPG, PNG, or WebP" |
| Duplicate detected | Caught by `UNIQUE(image_hash)` conflict on insert → notify user: "This receipt was already submitted (submission \<id\>)" |
| Claude returns invalid JSON | Strip markdown fences, retry parse; if still fails → open blank modal for full manual entry |
| Claude API error / timeout | Notify user: "Couldn't read your receipt automatically — opening manual entry"; open blank modal |
| Pydantic validation fails | Log the raw response for debugging; open blank modal for manual entry |
| Supabase insert/update error | Retry once; if still fails → mark row `status='error'` (or log if the row was never created), notify user to contact finance |
| Supabase unreachable | Notify user: "Couldn't save your receipt right now — please try again shortly"; log for retry |
| Slack profile lookup fails | Use Slack display name as fallback; leave email/department as "unknown" |

---

## Implementation Order

1. `utils/config.py` + `.env.example` — env var loading
2. `models/schemas.py` — Pydantic models
3. `migrations/001_reimbursements.sql` — apply the `reimbursements` table to Supabase
4. `app/main.py` — bare Slack Bolt server that acks `file_shared` events
5. `services/slack_helpers.py` — image download + profile lookup
6. `utils/image.py` — filetype check, conversion, hashing, base64 encoding
7. `services/supabase_store.py` — insert pending, dedup check, confirm update, per-user query
8. `services/vision.py` — Claude Vision call + Pydantic parsing
9. `app/listeners/file_upload.py` — full upload pipeline wired together
10. `app/views/modals.py` — Block Kit modal builder
11. `app/listeners/modal_submit.py` — confirmation handler → update row to confirmed
12. Finance notification message to Slack channel
13. Tests

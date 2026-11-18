# DataStory Slack Reimbursement Bot — Revised Architecture

## Overview

Members of DataStory@Berkeley DM receipt photos directly to the bot. The bot
normalizes the image, deduplicates via SHA-256 hashing, extracts structured data
via Claude Vision (structured outputs validated by Pydantic), presents an editable
confirmation modal, then writes the submission to Supabase (Postgres) and notifies
the finance team in a Slack channel.

DataStory is a club, not a corporation, so there is no department field anywhere
in the pipeline.

---

## Credentials to Obtain

Only two of these are true API keys; the rest are tokens or host-provided
connection strings.

| Service | Credential(s) | Where to obtain | Manual? |
|---|---|---|---|
| **Anthropic** (Claude Vision) | `ANTHROPIC_API_KEY` (`sk-ant-…`) | console.anthropic.com → API Keys | Yes — set a spend limit; billed per receipt parsed |
| **Slack** | `SLACK_BOT_TOKEN` (`xoxb-…`), `SLACK_APP_TOKEN` (`xapp-…`), `SLACK_SIGNING_SECRET` | api.slack.com/apps → *OAuth & Permissions* (bot token), *Basic Information → App-Level Tokens* (app token, scope `connections:write`), *Basic Information* (signing secret) | Yes — bot token issued when the app is installed with the scopes below; app token enables Socket Mode (no public URL needed) |
| **Supabase** | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | supabase.com → project → *Settings → API* | Yes — service-role key for server-side writes |

The Supabase migration removes the previous Google credentials
(`GOOGLE_SHEETS_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`) and the entire GCP project /
service-account / sheet-sharing setup.

---

## Tech Stack

- **Python 3.11+**
- **Slack Bolt** (`slack-bolt`) — event handling, modals, message composition; runs in **Socket Mode** (`SocketModeHandler`), so no public Request URL is required
- **Anthropic Python SDK** — Claude Vision for receipt parsing (model: `claude-sonnet-5`)
- **Supabase** (`supabase-py`) — Postgres storage for confirmed submissions,
  pending-submission state, and duplicate detection (native `UNIQUE` constraint)
- **Pydantic v2** — schema validation for Claude Vision responses
- **Pillow** + **pillow-heif** — decode every upload (incl. iPhone HEIC) and
  normalize it (EXIF rotation, downscale, auto-contrast) to PNG before Vision
- **pytest** — offline test suite (all external services faked)
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
Member DMs receipt image to the bot (message.im, channel_type "im")
                    │
                    ▼
         ┌────────────────────┐
         │   Slack Bolt App   │
         │   ack() < 3 sec    │──── ack over the Socket Mode WebSocket
         │ (background thread │      then process each file in a background
         │   per file)        │      thread; any unexpected error is caught,
         └─────────┬──────────┘      logged, and reported to the member
                   │
                   ▼
         ┌────────────────────┐
         │  Download Image    │
         │  via Slack API     │──── GET file URL with Authorization: Bearer <bot_token>
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Normalize Image   │──── EVERY upload goes through Pillow (HEIC via
         │  (Pillow → PNG)    │     pillow-heif): bake EXIF rotation, flatten mode,
         │                    │     downscale to 1568px long edge, auto-contrast,
         │                    │     re-encode PNG. Undecodable → "Unsupported
         └─────────┬──────────┘     image format" and stop
                   │
                   ▼
         ┌────────────────────┐
         │  SHA-256 Hash      │──── hash the *normalized* PNG bytes
         │  of image bytes    │     (deterministic, so re-uploads hash the same)
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Supabase: Dedup   │──── SELECT * FROM reimbursements WHERE image_hash = <sha256>
         │  Lookup            │     row exists → branch on status/owner (see
         └─────────┬──────────┘     "Duplicate & Re-upload Handling") and stop
                   │ (no existing row)
                   ▼
         ┌────────────────────┐
         │  Claude Vision     │──── messages.parse() with output_format=ReceiptExtraction
         │  (claude-sonnet-5) │     (structured outputs: schema-valid JSON by
         │                    │     construction — no fence stripping)
         │                    │     explicit: return null for unreadable fields
         └─────────┬──────────┘     ANY failure → blank extraction (manual entry)
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
         │  Row in Supabase   │     UNIQUE(image_hash) conflict (concurrent upload of
         │                    │     the same image) → same branch as the dedup lookup
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Post "Review &    │──── DM with a summary + button (value = submission_id).
         │  Submit" button    │     A modal can only be opened with a trigger_id, which
         └─────────┬──────────┘     only a user interaction (the click) provides
                   │ user clicks the button
                   ▼
         ┌────────────────────┐
         │  Status Check      │──── row missing → "please re-upload"
         │                    │     row confirmed → "already submitted", replace button
         └─────────┬──────────┘     row pending/error → continue
                   │
                   ▼
         ┌────────────────────┐
         │  Open Slack Modal  │──── single-step editable modal, pre-filled from the row
         │  (Block Kit)       │     null fields shown as empty inputs for manual entry
         │                    │     private_metadata = JSON {submission_id, channel, ts}
         └─────────┬──────────┘     (channel/ts locate the review message to update later)
                   │ user clicks "Submit Reimbursement"
                   ▼
         ┌────────────────────┐
         │  Validate Amounts  │──── inline field errors (modal stays open):
         │                    │     total required, > 0; tax optional, ≥ 0, ≤ total;
         │                    │     rejects nan/inf/negatives and values too large for
         └─────────┬──────────┘     numeric(12,2); accepts "$1,234.56"; rounds to cents
                   │ ack() closes the modal; the rest runs in a background thread
                   ▼
         ┌────────────────────┐
         │  Pull Slack        │──── users.info API → real_name (fallback display_name), email
         │  Profile Data      │     (requires users:read + users:read.email scopes)
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Confirm Row       │──── UPDATE reimbursements SET status='confirmed', vendor, date,
         │  (atomic guard)    │     total, ..., employee_name, email, submitted_at
         │                    │     WHERE submission_id=... AND status IN ('pending','error')
         │                    │     0 rows → already confirmed (double submit): DM
         │                    │     "already submitted", NO finance post, stop
         └─────────┬──────────┘     failure → status='error', DM "tap to retry"
                   │ row confirmed — nothing below can undo it
                   ▼
         ┌────────────────────┐
         │  Notify Finance    │──── post summary to #finance-reimbursements
         │  (best-effort)     │     (who, email, vendor, date, category, total, tax, ID);
         └─────────┬──────────┘     retried once, then logged at ERROR with the ID
                   │
                   ▼
         ┌────────────────────┐
         │  Confirm to User   │──── DM "Receipt filed — submission <id>" (best-effort)
         └─────────┬──────────┘
                   │
                   ▼
         ┌────────────────────┐
         │  Replace Button    │──── chat_update the review message → "✅ Submitted"
         │  (best-effort)     │     (Slack can't disable a button; the DB guard is what
         └────────────────────┘     actually prevents resubmission)
```

---

## Duplicate & Re-upload Handling

Dedup is keyed on the SHA-256 of the normalized image. When an upload's hash is
already on file (found by the lookup, or by an insert conflict when two uploads
of the same image race), the response depends on the existing row:

| Existing row | Same member re-uploads | Another member uploads it |
|---|---|---|
| `pending` | Re-post the **same** submission's Review & Submit button, pre-filled from the stored row | "Already uploaded by another member" (their submission ID is not revealed) |
| `error` | Same, with "your earlier attempt didn't go through — tap to try again" | Same as above |
| `confirmed` | "Already submitted (submission `<id>`)" | Same |

Re-offering reuses the existing row: no second Vision call, no orphaned rows. A
member may end up with two review messages pointing at one submission; that is
safe because confirmation is guarded atomically in the database, and a click on
a stale button replaces it with "Submitted".

Submission status lifecycle:

```
pending ──submit──▶ confirmed          (terminal for the bot; `reimbursed` is
   │                    ▲                  then flipped by hand after payout)
   └─DB failure─▶ error ┘ retry via the same button (or by re-uploading)
```

---

## Data Models (Pydantic)

```python
from datetime import date as _date  # aliased: a field named `date` would otherwise
                                     # shadow the type during Pydantic hint resolution
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
    `reimbursements` table (see schema below)."""
    submission_id: str          # uuid4
    user_id: str                # Slack user ID — kept for per-person querying
    file_id: str                # Slack file ID
    image_hash: str             # SHA-256 hex digest (UNIQUE in Postgres)
    extraction: ReceiptExtraction
    employee_name: Optional[str] = None
    employee_email: Optional[str] = None   # stored in the `email` column
    status: str = "pending"     # pending | confirmed | error
    reimbursed: bool = False    # set/toggled manually in Supabase after payout
    reimbursed_at: Optional[str] = None  # auto-stamped by a DB trigger on flip
    created_at: str             # ISO 8601 timestamp
```

---

## Supabase Schema

A single `reimbursements` table serves confirmed records, pending state, and dedup.
The `UNIQUE (image_hash)` constraint enforces duplicate detection natively — no
separate key or TTL is needed. Pending submissions are just rows with
`status = 'pending'`; the review button carries `submission_id` and the modal
re-reads the row when opened (so a re-offered or retried submission is
pre-filled from what was stored).

The schema is built by two migrations, applied in order via the Supabase SQL
editor: `001_reimbursements.sql` (the table and base indexes) and
`002_reimbursed_flag.sql` (the `reimbursed` / `reimbursed_at` columns, their
index, and the trigger that stamps `reimbursed_at`). The combined result:

```sql
create table reimbursements (
    submission_id   uuid        primary key default gen_random_uuid(),
    user_id         text        not null,
    file_id         text,
    employee_name   text,
    email           text,
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
    reimbursed      boolean     not null default false,
    reimbursed_at   timestamptz,
    constraint uq_reimbursements_image_hash unique (image_hash)
);

create index idx_reimbursements_user_id    on reimbursements (user_id);
create index idx_reimbursements_created_at on reimbursements (created_at);
create index idx_reimbursements_status     on reimbursements (status);
create index idx_reimbursements_reimbursed on reimbursements (reimbursed);
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
| vendor | Claude Vision / user edit | `text` |
| date | Claude Vision / user edit | `date` |
| total | Claude Vision / user edit (validated > 0, rounded to cents) | `numeric(12,2)` |
| currency | Claude Vision / user edit | `text` |
| category | Claude Vision / user edit | `text` |
| tax | Claude Vision / user edit | `numeric(12,2)` |
| line_items | Claude Vision / user edit | `jsonb` |
| image_hash | SHA-256 of normalized PNG bytes | `text` (UNIQUE) |
| status | Server (`pending` → `confirmed`; `error` on DB failure, retryable) | `text` |
| created_at | Server default | `timestamptz` |
| submitted_at | Set on confirmation | `timestamptz` |
| reimbursed | Set manually in Supabase after payout | `boolean` (default false) |
| reimbursed_at | Auto-stamped by trigger when `reimbursed` flips | `timestamptz` |

---

## Claude Vision Prompt

The call uses **structured outputs**: `client.messages.parse(...,
output_format=ReceiptExtraction)`. The JSON shape is enforced by the API from the
Pydantic model, so the prompt only needs to describe *how* to read the receipt.
The live prompt is `_PROMPT` in `services/vision.py`:

```
You are a receipt parser. The image is often a phone photo of a paper
receipt — expect perspective skew, glare, shadows, curl, and faint thermal-paper
print rather than a clean flatbed scan. Read carefully at angles and low contrast.
Extract the structured data defined by the response schema.

Rules:
- Do your best to read faint or angled text, but return null for a field only when
  it is genuinely illegible. Prefer null over a guess — never invent a value.
- "total" is the final amount paid including tax and tip.
- "category" should be one of: meals, travel, supplies, software, equipment, lodging, other.
- "date" must be ISO 8601 format (YYYY-MM-DD).
- If the receipt is not in English, still extract the data and translate the vendor name.
```

The category list must match `CATEGORIES` in `app/views/modals.py` (a test
enforces this); the modal only pre-selects a category that is in that list.

---

## Slack Configuration

### Required OAuth Scopes
- `files:read` — download uploaded receipt images
- `chat:write` — send confirmation and finance notifications, and update the
  review message to replace its button after submission
- `im:history` — receive receipt uploads sent to the bot via DM
- `im:write` — open/reply in the DM with the user
- `users:read` — pull the member's name
- `users:read.email` — pull the member's email for the record

### Event Subscriptions
- `message.im` — triggers the receipt processing pipeline. A DM to the bot is
  delivered as a `message` event with `channel_type: "im"` (subscribe to
  `message.im`; in Bolt, handle `message` and filter on `channel_type == "im"`
  with an attached file).

### App Home
- Enable the **Messages Tab** (App Home → Show Tabs) and allow users to send
  messages from it — otherwise members cannot DM the bot at all.

### Socket Mode & Interactivity
- Enable **Socket Mode** (Settings → Socket Mode). With Socket Mode on, events
  and interactivity arrive over an outbound WebSocket, so **no public Request URL
  is required** for either.
- Enable interactivity for the Review & Submit button and modal submissions (no
  Request URL needed under Socket Mode).
- Requires an app-level token (`SLACK_APP_TOKEN`, scope `connections:write`).

---

## Project Structure

```
datastory-bot/
├── app/
│   ├── __init__.py
│   ├── main.py              # Slack Bolt app init (Socket Mode), event/action routing
│   ├── listeners/
│   │   ├── __init__.py
│   │   ├── file_upload.py   # DM w/ file → download, normalize, hash, dedup/re-offer,
│   │   │                    #   extract, insert pending, post Review & Submit button
│   │   └── modal_submit.py  # button → status check + modal; submit → validate,
│   │                        #   confirm (guarded), notify finance, DM, replace button
│   └── views/
│       ├── __init__.py
│       └── modals.py        # Block Kit modal builder + private_metadata encode/decode
├── models/
│   ├── __init__.py
│   └── schemas.py           # Pydantic models: LineItem, ReceiptExtraction, Submission
├── services/
│   ├── __init__.py
│   ├── vision.py            # Claude Vision API call + response parsing
│   ├── supabase_store.py    # Supabase client: insert / find_by_hash / guarded confirm /
│   │                        #   mark_error / per-user queries
│   └── slack_helpers.py     # Image download, profile lookup, message posting
├── utils/
│   ├── __init__.py
│   ├── image.py             # Pillow/HEIC normalization → PNG, base64 encoding, SHA-256
│   └── config.py            # Environment variable loading (tokens, Supabase URL/key)
├── migrations/
│   ├── 001_reimbursements.sql  # reimbursements table DDL (see Supabase Schema)
│   └── 002_reimbursed_flag.sql # reimbursed / reimbursed_at + auto-stamp trigger
├── tests/
│   ├── __init__.py
│   ├── conftest.py             # fake env, in-memory Supabase, live-client guard
│   ├── test_schemas.py         # Pydantic model validation
│   ├── test_image.py           # normalization, EXIF rotation, hash determinism
│   ├── test_supabase_store.py  # insert / dedup / guarded confirm / retry from error
│   ├── test_vision.py          # Vision wrapper contract (mocked SDK)
│   ├── test_slack_helpers.py   # file download, profile lookup fallbacks
│   ├── test_modals.py          # modal builder, private_metadata round-trip
│   ├── test_file_upload.py     # intake pipeline end to end, re-upload matrix
│   ├── test_modal_submit.py    # button → modal → submit, validation, failure paths
│   └── manual_vision_check.py  # NOT a pytest test: hits the real Claude API
├── .env.example             # Template for required env vars
├── requirements.txt
├── Procfile                 # Railway/Render entry point (TODO)
└── README.md                # (TODO)
```

---

## Testing

```bash
python -m pytest tests -q
```

The suite runs fully offline. `tests/conftest.py` forces fake credentials into
the environment before any app module is imported (so the real `.env` is never
used), swaps Supabase for an in-memory fake that enforces `UNIQUE(image_hash)`
and the confirm status guard, and fails any test that tries to construct a live
Supabase or Anthropic client. Slack is a `MagicMock`; background threads run
inline so tests are deterministic.

Receipt-reading *accuracy* is deliberately out of scope for the unit suite; run
`python -m tests.manual_vision_check` against the real API for that.

---

## Environment Variables

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...             # app-level token for Socket Mode (scope: connections:write)
SLACK_SIGNING_SECRET=...
SLACK_FINANCE_CHANNEL_ID=C...       # #finance-reimbursements channel ID
ANTHROPIC_API_KEY=sk-ant-...
SUPABASE_URL=https://<project-ref>.supabase.co    # base project URL only — no /rest/v1/ suffix
SUPABASE_SERVICE_ROLE_KEY=...       # service-role key for server-side writes
```

---

## Error Handling Strategy

Guiding rule: a background thread never fails silently — every failure is logged
and the member is told what happened and what to do next. Once a row is
confirmed, nothing downstream can undo it.

**Intake (upload → review button)**

| Failure Point | Behavior |
|---|---|
| Image download fails (incl. Slack returning an HTML login page) | "Couldn't download your receipt — try re-uploading" |
| Image can't be decoded | "Unsupported image format — please upload JPG, PNG, or WebP" |
| Supabase unreachable at the dedup lookup | "Couldn't save your receipt right now — please try again shortly"; Vision is not called |
| Hash already on file | See "Duplicate & Re-upload Handling" |
| Claude API error / timeout / refusal / no structured output (`VisionParseError`) | Blank extraction → review button says "couldn't read automatically, enter manually" |
| Any other exception from the Vision call | Logged; same manual-entry fallback (losing auto-parse never blocks filing) |
| Supabase insert error (non-duplicate) | "Couldn't save your receipt right now — please try again shortly" |
| Anything else unexpected | Logged with the file ID; "Something went wrong processing your receipt — please try uploading it again" |

**Review & submit**

| Failure Point | Behavior |
|---|---|
| Button clicked for a missing row | "I couldn't find that submission — please re-upload the receipt" |
| Button clicked for a confirmed row | "Already submitted"; the stale button is replaced |
| Invalid total/tax | Inline field error; modal stays open; nothing written |
| Second submit of the same submission | Guarded UPDATE matches 0 rows → "already submitted"; no finance post, no data overwritten |
| Supabase confirm fails | Row marked `status='error'` (retryable); DM "tap Review & Submit again to retry, or contact finance" |
| Slack profile lookup fails | Name/email stored as null; finance message shows `<@user>` mention instead |
| Finance post fails | Retried once; then logged at ERROR (`FINANCE NOT NOTIFIED … <id>`). Row stays `confirmed`; member is still told it's filed |
| Member DM / button replacement fails | Logged only |

---

## Implementation Status

Done:

1. `utils/config.py` + `.env.example` — env var loading
2. `models/schemas.py` — Pydantic models
3. `migrations/001_reimbursements.sql`, `002_reimbursed_flag.sql`
4. `app/main.py` — Slack Bolt Socket Mode app, listener registration
5. `services/slack_helpers.py` — image download + profile lookup
6. `utils/image.py` — normalization, hashing, base64 encoding
7. `services/supabase_store.py` — insert pending, find_by_hash, guarded confirm, mark_error, per-user query
8. `services/vision.py` — Claude Vision call via structured outputs
9. `app/listeners/file_upload.py` — intake pipeline incl. re-upload handling
10. `app/views/modals.py` — Block Kit modal builder
11. `app/listeners/modal_submit.py` — status check, validation, guarded confirm, notifications
12. Finance notification message to Slack channel
13. Offline test suite (`tests/`)

Next:

14. Receipt-parsing accuracy on real phone photos (`tests/manual_vision_check.py`)
15. `Procfile` + `README.md`; deploy to Railway
16. Optional: `finance_notified_at` column so missed finance notifications can be
    queried and resent instead of only appearing in logs
17. Optional: `pg_cron` cleanup of long-abandoned `pending` rows

"""Shared test fixtures.

All tests run fully offline. Fake credentials are forced into the environment
*before* any app module is imported, so `utils.config` never picks up the real
`.env` (load_dotenv does not override variables that are already set). The real
Supabase / Anthropic client factories are replaced with a guard that fails the
test loudly if anything tries to build a live client.
"""

import os

_FAKE_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_APP_TOKEN": "xapp-test",
    "SLACK_SIGNING_SECRET": "test-secret",
    "SLACK_FINANCE_CHANNEL_ID": "CFINANCE",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "test-service-role",
}
os.environ.update(_FAKE_ENV)

import copy  # noqa: E402
import threading  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402
from postgrest.exceptions import APIError  # noqa: E402

from services import supabase_store, vision  # noqa: E402


# --------------------------------------------------------------------------- #
# Live-client guard
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _no_live_clients(monkeypatch):
    def _forbidden(*_a, **_k):
        raise AssertionError("test tried to create a live external client")

    monkeypatch.setattr(supabase_store, "_client", None)
    monkeypatch.setattr(supabase_store, "_get_client", _forbidden)
    monkeypatch.setattr(vision, "_client", None)
    monkeypatch.setattr(vision, "_get_client", _forbidden)


# --------------------------------------------------------------------------- #
# In-memory Supabase fake
# --------------------------------------------------------------------------- #

class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Minimal chainable stand-in for a postgrest query builder."""

    def __init__(self, db, table):
        self._db = db
        self._table = table
        self._op = None
        self._payload = None
        self._filters = []
        self._limit = None
        self._order = None

    def select(self, _cols="*"):
        self._op = "select"
        return self

    def insert(self, row):
        self._op, self._payload = "insert", row
        return self

    def update(self, updates):
        self._op, self._payload = "update", updates
        return self

    def eq(self, col, val):
        self._filters.append((col, lambda v, val=val: v == val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, lambda v, vals=tuple(vals): v in vals))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def _matches(self, row):
        return all(pred(row.get(c)) for c, pred in self._filters)

    def execute(self):
        rows = self._db.tables.setdefault(self._table, [])
        self._db.calls.append((self._table, self._op, self._payload,
                               [c for c, _ in self._filters]))

        if self._db.fail_next:
            exc, self._db.fail_next = self._db.fail_next, None
            raise exc

        if self._op == "insert":
            row = copy.deepcopy(self._payload)
            if any(r["image_hash"] == row["image_hash"] for r in rows):
                raise APIError({
                    "message": "duplicate key value violates unique constraint",
                    "code": "23505", "hint": None, "details": None,
                })
            row.setdefault("created_at", f"2026-01-01T00:00:{len(rows):02d}Z")
            row.setdefault("reimbursed", False)
            rows.append(row)
            return _Result([copy.deepcopy(row)])

        matched = [r for r in rows if self._matches(r)]
        if self._op == "update":
            for r in matched:
                r.update(copy.deepcopy(self._payload))
            return _Result(copy.deepcopy(matched))

        if self._order:
            col, desc = self._order
            matched.sort(key=lambda r: r.get(col) or "", reverse=desc)
        if self._limit is not None:
            matched = matched[: self._limit]
        return _Result(copy.deepcopy(matched))


class FakeSupabase:
    def __init__(self):
        self.tables = {}
        self.calls = []
        self.fail_next = None  # set to an exception to fail the next execute()

    def table(self, name):
        return _Query(self, name)

    def rows(self, table="reimbursements"):
        return self.tables.get(table, [])


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeSupabase()
    monkeypatch.setattr(supabase_store, "_get_client", lambda: db)
    return db


# --------------------------------------------------------------------------- #
# Slack client + threading helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def slack_client():
    """A MagicMock WebClient. users_info returns a realistic profile by default."""
    client = MagicMock(name="WebClient")
    client.users_info.return_value = {
        "user": {
            "real_name": "Ada Lovelace",
            "profile": {"real_name": "Ada Lovelace", "display_name": "ada",
                        "email": "ada@example.com"},
        }
    }
    return client


class _InlineThread:
    """Drop-in for threading.Thread that runs the target synchronously on start()."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture
def inline_threads(monkeypatch):
    """Make background threads in the listeners run inline so tests are deterministic."""
    monkeypatch.setattr(threading, "Thread", _InlineThread)


def posted_texts(client):
    """All `text=` values passed to client.chat_postMessage, in order."""
    return [c.kwargs.get("text") for c in client.chat_postMessage.call_args_list]

"""Persistent single-use drafts. Content is encrypted; claims are atomic across MCP processes."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from .storage import data_dir, protect, unprotect


@contextmanager
def database():
    conn = sqlite3.connect(data_dir() / "outbox.sqlite3", timeout=5)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, account TEXT NOT NULL, created REAL NOT NULL, state TEXT NOT NULL, payload BLOB NOT NULL, result BLOB)")
        with conn:
            yield conn
    finally:
        conn.close()


def create(account_id, payload):
    draft_id = str(uuid.uuid4())
    payload["random_id"] = (uuid.UUID(draft_id).int & ((1 << 63) - 1)) or 1
    with database() as conn:
        conn.execute("INSERT INTO drafts VALUES (?, ?, ?, 'ready', ?, NULL)",
                     (draft_id, str(account_id), time.time(), protect(json.dumps(payload).encode())))
    return draft_id


def get(draft_id, account_id):
    with database() as conn:
        row = conn.execute("SELECT created, state, payload, result FROM drafts WHERE id=? AND account=?", (draft_id, str(account_id))).fetchone()
    if row is None:
        return None
    return {"created": row[0], "state": row[1], "payload": json.loads(unprotect(row[2])),
            "result": json.loads(unprotect(row[3])) if row[3] else None}


def claim(draft_id, account_id):
    with database() as conn:
        return conn.execute("UPDATE drafts SET state='sending' WHERE id=? AND account=? AND state='ready' AND created>=?",
                            (draft_id, str(account_id), time.time() - 1800)).rowcount == 1


def finish(draft_id, account_id, state, result):
    with database() as conn:
        conn.execute("UPDATE drafts SET state=?, result=? WHERE id=? AND account=? AND state='sending'",
                     (state, protect(json.dumps(result).encode()), draft_id, str(account_id)))

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS history_cursor (
 account_hash TEXT PRIMARY KEY, history_id TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS context_facts (
 account_hash TEXT NOT NULL, kind TEXT NOT NULL, subject_key TEXT NOT NULL,
 topic TEXT NOT NULL DEFAULT '', occurred_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 PRIMARY KEY(account_hash, kind, subject_key, topic)
);
CREATE INDEX IF NOT EXISTS context_lookup ON context_facts(account_hash,kind,subject_key,expires_at);
"""
def opaque(value: str) -> str: return hashlib.sha256(value.encode()).hexdigest()

def scoped_key(account: str, kind: str, value: str) -> str:
    return opaque(account.casefold() + "\0" + kind + "\0" + value.strip().casefold())

class TriageStore:
    def __init__(self, path: str | Path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path); self.db.executescript(_SCHEMA)
        os.chmod(self.path, 0o600)
    def close(self): self.db.close()
    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    def set_cursor(self, account: str, history_id: str):
        with self.db: self.db.execute("INSERT INTO history_cursor(account_hash,history_id) VALUES(?,?) ON CONFLICT(account_hash) DO UPDATE SET history_id=excluded.history_id,updated_at=CURRENT_TIMESTAMP",(opaque(account.casefold()),history_id))
    def get_cursor(self, account: str) -> str | None:
        row=self.db.execute("SELECT history_id FROM history_cursor WHERE account_hash=?",(opaque(account.casefold()),)).fetchone(); return row[0] if row else None
    def put_fact(self, account: str, kind: str, value: str, occurred_at: int, expires_at: int, topic: str = ""):
        if not value or expires_at <= occurred_at: return
        ah, key = opaque(account.casefold()), scoped_key(account,kind,value)
        with self.db:
            self.db.execute("""INSERT INTO context_facts(account_hash,kind,subject_key,topic,occurred_at,expires_at)
              VALUES(?,?,?,?,?,?) ON CONFLICT(account_hash,kind,subject_key,topic) DO UPDATE SET
              occurred_at=max(occurred_at,excluded.occurred_at),expires_at=max(expires_at,excluded.expires_at)""",
              (ah,kind,key,topic,occurred_at,expires_at))
    def has_fact(self, account: str, kind: str, value: str, now: int) -> bool:
        row=self.db.execute("SELECT 1 FROM context_facts WHERE account_hash=? AND kind=? AND subject_key=? AND expires_at>? LIMIT 1",
          (opaque(account.casefold()),kind,scoped_key(account,kind,value),now)).fetchone()
        return bool(row)

    def topics_for(self, account: str, kind: str, value: str, now: int) -> tuple[str,...]:
        rows=self.db.execute("SELECT DISTINCT topic FROM context_facts WHERE account_hash=? AND kind=? AND subject_key=? AND expires_at>? AND topic<>'' ORDER BY topic LIMIT 5",
          (opaque(account.casefold()),kind,scoped_key(account,kind,value),now)).fetchall()
        return tuple(r[0] for r in rows)

    def prune_context(self, account: str, now: int, max_rows: int = 5000):
        ah=opaque(account.casefold())
        with self.db:
            self.db.execute("DELETE FROM context_facts WHERE account_hash=? AND expires_at<=?",(ah,now))
            self.db.execute("""DELETE FROM context_facts WHERE rowid IN (
              SELECT rowid FROM context_facts WHERE account_hash=? ORDER BY occurred_at DESC LIMIT -1 OFFSET ?)""",(ah,max_rows))
    def context_count(self, account: str) -> int:
        return int(self.db.execute("SELECT count(*) FROM context_facts WHERE account_hash=?",(opaque(account.casefold()),)).fetchone()[0])

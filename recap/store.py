"""Bounded, transactional local state. No Codex files are modified here."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, data_dir: str | Path, max_sources: int = 600, max_summaries: int = 48):
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.max_sources, self.max_summaries = max_sources, max_summaries
        self.db = sqlite3.connect(path / "recap.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, timestamp REAL NOT NULL,
                hash TEXT NOT NULL, generated_hash TEXT, eligible INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS summaries (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, timestamp REAL NOT NULL,
                read_at REAL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL, timestamp REAL NOT NULL);
        """)
        self.db.commit()

    def get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def upsert_sources(self, sources: list[dict], now: float) -> None:
        with self.lock, self.db:
            for source in sources:
                self.db.execute("""INSERT INTO sources (id,payload,timestamp,hash,eligible) VALUES (?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,timestamp=excluded.timestamp,
                    hash=excluded.hash,eligible=excluded.eligible""",
                    (source["id"], json.dumps(source, ensure_ascii=False), source["timestamp"],
                     source["hash"], int(source.get("eligible", False))))
            self.db.execute("DELETE FROM sources WHERE timestamp < ?", (now - 14 * 86400,))
            self.db.execute("DELETE FROM sources WHERE id NOT IN (SELECT id FROM sources ORDER BY timestamp DESC LIMIT ?)", (self.max_sources,))

    def sources(self, start: float = 0, end: float = float("inf"), new_only: bool = False) -> list[dict]:
        with self.lock:
            query = "SELECT payload FROM sources WHERE timestamp>=? AND timestamp<?"
            if new_only:
                query += " AND eligible=1 AND (generated_hash IS NULL OR generated_hash!=hash)"
            query += " ORDER BY timestamp ASC,id ASC"
            return [json.loads(row[0]) for row in self.db.execute(query, (start, end))]

    def save_summary(self, summary: dict, source_hashes: dict[str, str], now: float) -> None:
        """Result and progress advance together; an error never consumes sources."""
        with self.lock, self.db:
            self.db.execute("INSERT INTO summaries VALUES (?,?,?,NULL)",
                            (summary["id"], json.dumps(summary, ensure_ascii=False), now))
            for source_id, digest in source_hashes.items():
                self.db.execute("UPDATE sources SET generated_hash=? WHERE id=? AND hash=?", (digest, source_id, digest))
            for key, value in (("last_generated_at", now), ("last_review_at", now), ("retry_after", 0), ("last_error", None)):
                self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))
            self.db.execute("DELETE FROM summaries WHERE id NOT IN (SELECT id FROM summaries ORDER BY timestamp DESC LIMIT ?)", (self.max_summaries,))

    def consume_without_surface(self, source_hashes: dict[str, str], now: float) -> None:
        with self.lock, self.db:
            for source_id, digest in source_hashes.items():
                self.db.execute("UPDATE sources SET generated_hash=? WHERE id=? AND hash=?", (digest, source_id, digest))
            for key, value in (("last_review_at", now), ("retry_after", 0), ("last_error", None)):
                self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def unread_summaries(self) -> list[dict]:
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM summaries WHERE read_at IS NULL ORDER BY timestamp DESC LIMIT 12")]

    def latest_summary(self) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT payload,read_at FROM summaries ORDER BY timestamp DESC,rowid DESC LIMIT 1").fetchone()
            if not row:
                return None
            result = json.loads(row[0])
            result["unread"] = row[1] is None
            return result

    def recent_summaries(self, limit: int = 12) -> list[dict]:
        """Read bounded generation history independently of presentation/read state."""
        with self.lock:
            rows = self.db.execute("SELECT payload,read_at FROM summaries ORDER BY timestamp DESC,rowid DESC LIMIT ?",
                                   (max(0, min(12, int(limit))),))
            result = []
            for payload, read_at in rows:
                summary = json.loads(payload)
                summary["unread"] = read_at is None
                result.append(summary)
            return result

    def summary(self, summary_id: str | None) -> dict | None:
        if not summary_id:
            return None
        with self.lock:
            row = self.db.execute("SELECT payload,read_at FROM summaries WHERE id=?", (summary_id,)).fetchone()
            if not row:
                return None
            result = json.loads(row[0])
            result["unread"] = row[1] is None
            return result

    def mark_seen(self, summary_id: str, now: float) -> bool:
        with self.lock, self.db:
            changed = self.db.execute("UPDATE summaries SET read_at=COALESCE(read_at,?) WHERE id=?", (now, summary_id)).rowcount
            return bool(changed)

    def event(self, payload: dict, now: float) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO events(payload,timestamp) VALUES (?,?)", (json.dumps(payload, ensure_ascii=False), now))
            self.db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 128)")

    def stats(self) -> dict:
        with self.lock:
            count, pending = self.db.execute("SELECT COUNT(*),COALESCE(SUM(eligible=0),0) FROM sources").fetchone()
            unread = self.db.execute("SELECT COUNT(*) FROM summaries WHERE read_at IS NULL").fetchone()[0]
            return {"sources_total": count, "pending_turns": pending, "unread_summaries": unread}

    def close(self) -> None:
        with self.lock:
            self.db.close()

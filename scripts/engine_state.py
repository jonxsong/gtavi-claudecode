"""Durable local state for the Shorts service."""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("a UTC or offset-aware timestamp is required")
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("a UTC or offset-aware timestamp is required")
    return date.astimezone(timezone.utc)


def read_json(path):
    with path.open(encoding="utf-8-sig") as source:
        return json.load(source)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def sha256(path):
    with path.open("rb") as source:
        digest = hashlib.sha256()
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS service (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS streams (
    id TEXT PRIMARY KEY, creator TEXT NOT NULL, started TEXT NOT NULL,
    last_seen TEXT NOT NULL, ended TEXT, eligible INTEGER NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS moments (
    id TEXT PRIMARY KEY, creator TEXT NOT NULL, last_seen TEXT NOT NULL,
    eligible INTEGER NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_signal (
    stream_id TEXT NOT NULL, at INTEGER NOT NULL, count INTEGER NOT NULL, reactions INTEGER NOT NULL,
    PRIMARY KEY (stream_id, at)
);
CREATE TABLE IF NOT EXISTS native_assets (job_digest TEXT PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, digest TEXT NOT NULL, manifest TEXT NOT NULL, state TEXT NOT NULL,
    updated REAL NOT NULL, next_attempt REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0, reason TEXT, candidate TEXT, output_digest TEXT,
    metadata TEXT, session_url TEXT, video_id TEXT UNIQUE, reserved_day TEXT, published TEXT
);
CREATE INDEX IF NOT EXISTS job_work ON jobs(state, next_attempt);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY, time TEXT NOT NULL, job_id TEXT, action TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    job_id TEXT NOT NULL, hours INTEGER NOT NULL, observed TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (job_id, hours)
);
CREATE TABLE IF NOT EXISTS money (
    kind TEXT NOT NULL, reference TEXT NOT NULL, date TEXT NOT NULL, cents INTEGER NOT NULL,
    evidence TEXT NOT NULL, PRIMARY KEY (kind, reference)
);
"""


class State:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "state.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def exclusive_run(self):
        lock = sqlite3.connect(self.root / "worker.sqlite3", timeout=0)
        try:
            try:
                lock.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise RuntimeError("a service instance is already using this state directory") from exc
            yield
        finally:
            lock.close()

    def set(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO service VALUES (?, ?)", (key, json_text(value)))

    def audit(self, db, job_id, action, detail=None):
        db.execute("INSERT INTO audit(time,job_id,action,detail) VALUES (?,?,?,?)",
                   (now().isoformat(), job_id, action, detail))

    def register(self, job_id, manifest, digest):
        with self.connect() as db:
            found = db.execute("SELECT digest,state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if found is None:
                db.execute("INSERT INTO jobs(id,digest,manifest,state,updated) VALUES (?,?,?,'queued',?)",
                           (job_id, digest, json_text(manifest), time.time()))
                self.audit(db, job_id, "queued")
            elif found["digest"] != digest and found["state"] in {"queued", "blocked", "rejected"}:
                db.execute("""UPDATE jobs SET digest=?,manifest=?,state='queued',updated=?,
                           attempts=0,next_attempt=0,reason=NULL WHERE id=?""",
                           (digest, json_text(manifest), time.time(), job_id))
                self.audit(db, job_id, "manifest replaced")

    def due(self, states, limit=12):
        placeholders = ",".join("?" for _ in states)
        with self.connect() as db:
            return db.execute(f"SELECT * FROM jobs WHERE state IN ({placeholders}) AND next_attempt <= ? "
                              "ORDER BY next_attempt,updated LIMIT ?", (*states, time.time(), limit)).fetchall()

    def update(self, db, job_id, stage, reason=None, retry=0, **values):
        columns = {"attempts", "candidate", "metadata", "output_digest", "session_url", "video_id",
                   "reserved_day", "published"}
        if values.keys() - columns:
            raise ValueError("unknown job fields")
        changes = ",".join(f"{field}=?" for field in values)
        db.execute(f"UPDATE jobs SET state=?,reason=?,updated=?,next_attempt=?{',' if changes else ''}{changes} "
                   "WHERE id=?", (stage, reason, time.time(), time.time() + retry, *values.values(), job_id))
        self.audit(db, job_id, stage, reason)

    def retry(self, job, stage, reason, delay=None):
        attempt = job["attempts"] + 1
        if delay is None:
            delay = min(3600, 10 * 2 ** min(attempt, 9))
        with self.connect() as db:
            self.update(db, job["id"], stage, reason, retry=delay, attempts=attempt)

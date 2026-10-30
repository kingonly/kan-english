"""SQLite state in the data folder: the shows followed and their episodes."""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
  id TEXT PRIMARY KEY,             -- the p-<id> of the show page
  url TEXT NOT NULL,
  title_he TEXT, title_en TEXT, synopsis_en TEXT, image TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  added_at REAL, checked_at REAL
);
CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY,             -- the episode page id
  show_id TEXT NOT NULL,
  season INTEGER, episode INTEGER,
  url TEXT NOT NULL,
  title_he TEXT, title_en TEXT, duration TEXT,
  stream TEXT,                     -- HLS master
  status TEXT NOT NULL,            -- wanted | downloading | reading | translating | embedding | ready | failed
  note TEXT, file_path TEXT, cues INTEGER,
  cost REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""

_lock = threading.RLock()
_local = threading.local()
_path: Path | None = None


def configure(path: Path) -> None:
    global _path
    _path = path


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        assert _path is not None, "db.configure() first"
        _path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(_path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        _local.c = c
    return c


@contextmanager
def tx():
    with _lock:
        c = conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def now() -> float:
    return time.time()


def show(sid: str):
    return conn().execute("SELECT * FROM shows WHERE id=?", (sid,)).fetchone()


def shows() -> list:
    return conn().execute("SELECT * FROM shows ORDER BY added_at").fetchall()


def episode(eid: str):
    return conn().execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()


def episodes(where: str = "1", params: tuple = ()) -> list:
    return conn().execute(f"SELECT * FROM episodes WHERE {where} ORDER BY show_id, season, episode, id", params).fetchall()


def update(eid: str, **fields) -> None:
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE episodes SET {cols} WHERE id=?", (*fields.values(), eid))

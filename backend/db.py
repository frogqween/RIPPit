from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List
from datetime import datetime

from .settings import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    job_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    mode TEXT NOT NULL, -- 'audio' or 'video'
    container TEXT NOT NULL,
    site TEXT,
    title TEXT,
    video_id TEXT,
    status TEXT NOT NULL, -- queued, extracting, downloading, postprocessing, done, error
    filepath TEXT,
    playlist_title TEXT,
    playlist_index INTEGER,
    total_items INTEGER,
    started_at TEXT,
    finished_at TEXT,
    duration_sec REAL,
    error TEXT
);
"""

REQUIRED_ON_INSERT = ["job_id", "url", "mode", "container", "status", "started_at"]
ALL_FIELDS = [
    "job_id", "url", "mode", "container", "site", "title", "video_id",
    "status", "filepath", "playlist_title", "playlist_index", "total_items",
    "started_at", "finished_at", "duration_sec", "error",
]


def _connect() -> sqlite3.Connection:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(settings.DB_FILE, check_same_thread=False)


def init_db() -> None:
    with _connect() as con:
        con.execute(SCHEMA)
        con.commit()


def insert_job(meta: Dict[str, Any]) -> None:
    # Ensure required fields
    missing = [k for k in REQUIRED_ON_INSERT if not meta.get(k)]
    if missing:
        raise ValueError(f"Missing required fields for insert: {missing}")
    cols = REQUIRED_ON_INSERT + [
        "site", "title", "video_id", "filepath", "playlist_title", "playlist_index",
        "total_items", "finished_at", "duration_sec", "error",
    ]
    row = {k: meta.get(k) for k in cols}
    with _connect() as con:
        placeholders = ", ".join(["?"] * len(cols))
        con.execute(
            f"INSERT OR REPLACE INTO downloads ({', '.join(cols)}) VALUES ({placeholders})",
            [row[k] for k in cols],
        )
        con.commit()


def update_job(job_id: str, meta: Dict[str, Any]) -> None:
    # Update only provided keys (excluding job_id)
    keys = [k for k in ALL_FIELDS if k != "job_id" and (k in meta and meta[k] is not None)]
    if not keys:
        return
    set_expr = ", ".join([f"{k}=?" for k in keys])
    values = [meta[k] for k in keys] + [job_id]
    with _connect() as con:
        con.execute(f"UPDATE downloads SET {set_expr} WHERE job_id=?", values)
        con.commit()


def history(limit: int = 200) -> List[Dict[str, Any]]:
    with _connect() as con:
        cur = con.execute(
            "SELECT job_id, url, mode, container, site, title, video_id, status, filepath,"
            " playlist_title, playlist_index, total_items, started_at, finished_at, duration_sec, error"
            " FROM downloads ORDER BY COALESCE(finished_at, started_at) DESC LIMIT ?",
            (limit,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

"""SQLite history of checks. No accounts: everything is local."""
from __future__ import annotations

import json
import sqlite3
import time

from .config import DATA_DIR

DB_PATH = DATA_DIR / "gradeorskip.db"


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS checks (
        id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        batch_id TEXT,
        label TEXT,
        card TEXT,
        result TEXT NOT NULL,
        prices TEXT
    )""")
    return c


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    return {
        "id": r["id"], "created_at": r["created_at"], "batch_id": r["batch_id"], "label": r["label"],
        "card": json.loads(r["card"]) if r["card"] else None,
        "result": json.loads(r["result"]),
        "prices": json.loads(r["prices"]) if r["prices"] else {},
    }


def save_check(result: dict, card: dict | None = None, prices: dict | None = None,
               batch_id: str | None = None, label: str | None = None) -> None:
    with conn() as c:
        c.execute("INSERT INTO checks (id, created_at, batch_id, label, card, result, prices) VALUES (?,?,?,?,?,?,?)",
                  (result["id"], time.time(), batch_id, label, json.dumps(card) if card else None,
                   json.dumps(result), json.dumps(prices or {})))


def get_check(check_id: str) -> dict | None:
    with conn() as c:
        return _row(c.execute("SELECT * FROM checks WHERE id = ?", (check_id,)).fetchone())


def update_check(check_id: str, card: dict | None = None, prices: dict | None = None) -> None:
    with conn() as c:
        if card is not None:
            c.execute("UPDATE checks SET card = ? WHERE id = ?", (json.dumps(card), check_id))
        if prices is not None:
            c.execute("UPDATE checks SET prices = ? WHERE id = ?", (json.dumps(prices), check_id))


def list_checks(limit: int = 100, batch_id: str | None = None) -> list[dict]:
    with conn() as c:
        if batch_id:
            rows = c.execute("SELECT * FROM checks WHERE batch_id = ? ORDER BY created_at", (batch_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM checks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]

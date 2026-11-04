"""Request history in SQLite: what was heard, what ran, what was said back."""
import json
import os
import sqlite3
import time
from pathlib import Path


class History:
    def __init__(self, path: Path, limit: int = 500):
        # Every transcript is in here, so it is as private as the key in config.json. SQLite gives
        # its journal the mode of the database.
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600))
        path.chmod(0o600)
        self.db = sqlite3.connect(path)
        self.limit = limit
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, ts REAL NOT NULL,"
            " heard TEXT NOT NULL, reply TEXT NOT NULL, actions TEXT NOT NULL, status TEXT NOT NULL)")
        self.db.commit()

    def add(self, heard: str, reply: str, actions: list, status: str) -> int:
        cur = self.db.execute(
            "INSERT INTO requests (ts, heard, reply, actions, status) VALUES (?,?,?,?,?)",
            (time.time(), heard, reply, json.dumps(actions), status))
        self.db.execute(
            "DELETE FROM requests WHERE id NOT IN (SELECT id FROM requests ORDER BY id DESC LIMIT ?)",
            (self.limit,))
        self.db.commit()
        return cur.lastrowid

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, ts, heard, reply, actions, status FROM requests ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [{"id": r[0], "ts": r[1], "heard": r[2], "reply": r[3],
                 "actions": json.loads(r[4]), "status": r[5]} for r in rows]

    def clear(self) -> None:
        self.db.execute("DELETE FROM requests")
        self.db.commit()

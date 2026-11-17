"""Accounts, invites and file records: SQLite in DATA_DIR by default, or Postgres given a postgresql:// URL.

The SQL is written once, with ? placeholders, in the subset both databases understand.
"""
import sqlite3
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, hash TEXT NOT NULL,
       role TEXT NOT NULL, status TEXT NOT NULL, created BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS invites (token TEXT PRIMARY KEY, email TEXT NOT NULL, expires BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
       size BIGINT NOT NULL, type TEXT NOT NULL, created BIGINT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS files_user ON files (user_id)",
]
Conflict = (sqlite3.IntegrityError, psycopg.IntegrityError)  # a unique value that is already taken
Down = (sqlite3.OperationalError, psycopg.OperationalError)  # can't connect, locked too long, server gone


def is_postgres(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://"))


class DB:
    def __init__(self, url: str, sqlite_path):
        self.url, self.pg, self.path = url, is_postgres(url), sqlite_path

    @contextmanager
    def connect(self):
        """One transaction: committed when the block ends, rolled back if it raises."""
        if self.pg:
            # shortcut: a new connection per request (a few ms); add psycopg_pool if Postgres latency shows
            with psycopg.connect(self.url, connect_timeout=5, row_factory=dict_row) as c:
                yield c
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.pg else sql

    def all(self, sql: str, *args) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute(self._sql(sql), args).fetchall()]

    def one(self, sql: str, *args) -> dict | None:
        rows = self.all(sql, *args)
        return rows[0] if rows else None

    def run(self, sql: str, *args) -> int:
        """Run a change; returns how many rows it touched."""
        with self.connect() as c:
            return c.execute(self._sql(sql), args).rowcount

    def reserve(self, user_id: str, rows: list[tuple], limit: int) -> bool:
        """Record new files (id, name, size, type, created) for a user if they fit in `limit` bytes (0: no limit).
        The check and the insert hold a lock on the account, so uploads arriving together can't all pass the check."""
        with self.connect() as c:
            if self.pg:
                c.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
            else:
                c.execute("BEGIN IMMEDIATE")  # SQLite: take the write lock before reading
            used = c.execute(self._sql("SELECT COALESCE(SUM(size), 0) AS n FROM files WHERE user_id = ?"), (user_id,)).fetchone()
            if limit and used["n"] + sum(r[2] for r in rows) > limit:
                return False
            for fid, name, size, kind, created in rows:
                c.execute(self._sql("INSERT INTO files (id, user_id, name, size, type, created) VALUES (?, ?, ?, ?, ?, ?)"),
                          (fid, user_id, name, size, kind, created))
            return True

    def init(self) -> None:
        with self.connect() as c:
            if self.pg:  # every worker runs this at start; Postgres's IF NOT EXISTS is not safe against itself
                c.execute("SELECT pg_advisory_xact_lock(7300431)")
            else:
                c.execute("PRAGMA journal_mode=WAL")  # readers don't wait for a writer
            for statement in SCHEMA:
                c.execute(statement)

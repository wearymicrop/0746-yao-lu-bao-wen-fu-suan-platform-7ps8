from contextlib import contextmanager
import json
import sqlite3

from app.errors import BusinessError
from app.manifest import canonical


class Ledger:
    def __init__(self, path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS lockouts (
                    id TEXT PRIMARY KEY,
                    batch TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def source(self, identifier):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM sources WHERE id = ?", (identifier,)
            ).fetchone()
        if row is None:
            raise BusinessError("SOURCE_NOT_FOUND", "未找到数据来源", 404)
        return json.loads(row[0])

    def put_source(self, identifier, payload):
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO sources VALUES (?, ?)",
                (identifier, canonical(payload)),
            )
            return cursor.rowcount == 0

    def run(self, identifier):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM runs WHERE id = ?", (identifier,)
            ).fetchone()
        if row is None:
            raise BusinessError("RUN_NOT_FOUND", "未找到归档记录", 404)
        return json.loads(row[0])

    def put_run(self, payload):
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO runs VALUES (?, ?)",
                (payload["id"], canonical(payload)),
            )
            return cursor.rowcount == 0

    def lockout(self, identifier):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM lockouts WHERE id = ?", (identifier,)
            ).fetchone()
        if row is None:
            raise BusinessError("LOCKOUT_NOT_FOUND", "未找到复采闭锁记录", 404)
        return json.loads(row[0])

    def lockout_optional(self, identifier):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload FROM lockouts WHERE id = ?", (identifier,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def lockouts_for_batch(self, batch):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM lockouts WHERE batch = ? ORDER BY id", (batch,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def put_lockout(self, payload):
        """同一边界重复建账返回 reused=True，不新增记录、不重置计时。"""
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO lockouts VALUES (?, ?, ?)",
                (payload["id"], payload["batch"], canonical(payload)),
            )
            return cursor.rowcount == 0

    def update_lockout(self, identifier, payload):
        with self.connect() as conn:
            conn.execute(
                "UPDATE lockouts SET payload = ?, batch = ? WHERE id = ?",
                (canonical(payload), payload["batch"], identifier),
            )

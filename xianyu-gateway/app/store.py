"""SQLite 持久化：事件队列、会话消息、转人工记录。

事件状态流转：
  collecting（防抖合并中）→ pending（待推送）→ pushed（已推送待 ack）→ acked
  推送多次失败 → failed；推送后超时未 ack → timeout
"""
from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    item_id TEXT,
    buyer_id TEXT,
    buyer_name TEXT,
    message TEXT NOT NULL,
    msg_time TEXT,
    status TEXT NOT NULL,
    push_attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL,
    last_error TEXT,
    result TEXT,
    note TEXT,
    sent_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    ready_at REAL NOT NULL,
    pushed_at REAL,
    acked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_events_chat ON events(account_id, chat_id, status);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    event_id TEXT,
    shadow INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(account_id, chat_id, id);

CREATE TABLE IF NOT EXISTS seen_hooks (
    key TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    summary TEXT,
    created_at REAL NOT NULL
);
"""

# 仍占用会话（同一会话串行）的状态
ACTIVE_STATUSES = ("pending", "pushed")


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    # ---------- hook 入口 ----------

    async def record_buyer_message(self, payload: dict[str, Any], debounce_seconds: int) -> str | None:
        """记录买家消息并合并进防抖中的事件；重复推送返回 None。"""
        assert self.db
        account_id = str(payload.get("account_id") or "")
        chat_id = str(payload.get("chat_id") or "")
        message = str(payload.get("message") or "").strip()
        msg_time = str(payload.get("msg_time") or "")
        now = time.time()

        key = hashlib.sha256(f"{account_id}|{chat_id}|{message}|{msg_time}".encode()).hexdigest()
        cursor = await self.db.execute("INSERT OR IGNORE INTO seen_hooks(key, created_at) VALUES (?, ?)", (key, now))
        if cursor.rowcount == 0:
            await self.db.commit()
            return None

        msg_cursor = await self.db.execute(
            "INSERT INTO messages(account_id, chat_id, role, text, created_at) VALUES (?, ?, 'buyer', ?, ?)",
            (account_id, chat_id, message, now),
        )
        message_rowid = msg_cursor.lastrowid

        row = await (
            await self.db.execute(
                "SELECT id, message FROM events WHERE account_id=? AND chat_id=? AND status='collecting' "
                "ORDER BY created_at DESC LIMIT 1",
                (account_id, chat_id),
            )
        ).fetchone()
        if row:
            event_id = row["id"]
            await self.db.execute(
                "UPDATE events SET message=?, msg_time=?, ready_at=?, item_id=COALESCE(NULLIF(?, ''), item_id) WHERE id=?",
                (f"{row['message']}\n{message}", msg_time, now + debounce_seconds, str(payload.get("item_id") or ""), event_id),
            )
        else:
            event_id = f"evt_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
            await self.db.execute(
                "INSERT INTO events(id, account_id, chat_id, item_id, buyer_id, buyer_name, message, msg_time, "
                "status, created_at, ready_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'collecting', ?, ?)",
                (
                    event_id,
                    account_id,
                    chat_id,
                    str(payload.get("item_id") or ""),
                    str(payload.get("send_user_id") or ""),
                    str(payload.get("send_user_name") or ""),
                    message,
                    msg_time,
                    now,
                    now + debounce_seconds,
                ),
            )
        await self.db.execute("UPDATE messages SET event_id=? WHERE id=?", (event_id, message_rowid))
        await self.db.commit()
        return event_id

    # ---------- 推送调度 ----------

    async def promote_ready_events(self) -> None:
        """防抖结束、且同会话没有进行中事件的 collecting 事件 → pending。"""
        assert self.db
        now = time.time()
        await self.db.execute(
            "UPDATE events SET status='pending', next_attempt_at=? WHERE status='collecting' AND ready_at<=? "
            "AND NOT EXISTS (SELECT 1 FROM events e2 WHERE e2.account_id=events.account_id "
            "AND e2.chat_id=events.chat_id AND e2.status IN ('pending','pushed'))",
            (now, now),
        )
        await self.db.commit()

    async def due_pending_events(self) -> list[aiosqlite.Row]:
        assert self.db
        cursor = await self.db.execute(
            "SELECT * FROM events WHERE status='pending' AND next_attempt_at<=? ORDER BY created_at LIMIT 20",
            (time.time(),),
        )
        return list(await cursor.fetchall())

    async def mark_pushed(self, event_id: str) -> None:
        assert self.db
        await self.db.execute(
            "UPDATE events SET status='pushed', pushed_at=?, push_attempts=push_attempts+1, last_error=NULL WHERE id=?",
            (time.time(), event_id),
        )
        await self.db.commit()

    async def mark_push_failed(self, event_id: str, error: str, max_attempts: int) -> tuple[int, bool]:
        """记录一次推送失败，返回 (已尝试次数, 是否放弃)。"""
        assert self.db
        row = await (await self.db.execute("SELECT push_attempts FROM events WHERE id=?", (event_id,))).fetchone()
        attempts = (row["push_attempts"] if row else 0) + 1
        give_up = attempts >= max_attempts
        backoff = [5, 15, 60, 300, 900][min(attempts - 1, 4)]
        await self.db.execute(
            "UPDATE events SET push_attempts=?, last_error=?, status=?, next_attempt_at=? WHERE id=?",
            (attempts, error[:500], "failed" if give_up else "pending", time.time() + backoff, event_id),
        )
        await self.db.commit()
        return attempts, give_up

    async def expire_unacked(self, ack_timeout_seconds: int) -> list[str]:
        assert self.db
        deadline = time.time() - ack_timeout_seconds
        rows = await (
            await self.db.execute("SELECT id FROM events WHERE status='pushed' AND pushed_at<?", (deadline,))
        ).fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            await self.db.executemany("UPDATE events SET status='timeout' WHERE id=?", [(i,) for i in ids])
            await self.db.commit()
        return ids

    # ---------- 工具使用 ----------

    async def get_event(self, event_id: str) -> aiosqlite.Row | None:
        assert self.db
        return await (await self.db.execute("SELECT * FROM events WHERE id=?", (event_id,))).fetchone()

    async def chat_history(self, account_id: str, chat_id: str, limit: int) -> list[dict[str, Any]]:
        assert self.db
        rows = await (
            await self.db.execute(
                "SELECT role, text, shadow, created_at FROM messages WHERE account_id=? AND chat_id=? "
                "ORDER BY id DESC LIMIT ?",
                (account_id, chat_id, limit),
            )
        ).fetchall()
        return [
            {
                "role": row["role"],
                "text": row["text"],
                "shadow": bool(row["shadow"]),
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["created_at"])),
            }
            for row in reversed(rows)
        ]

    async def record_seller_message(self, event: aiosqlite.Row, text: str, shadow: bool) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO messages(account_id, chat_id, role, text, event_id, shadow, created_at) "
            "VALUES (?, ?, 'seller_bot', ?, ?, ?, ?)",
            (event["account_id"], event["chat_id"], text, event["id"], int(shadow), time.time()),
        )
        await self.db.execute("UPDATE events SET sent_count=sent_count+1 WHERE id=?", (event["id"],))
        await self.db.commit()

    async def record_handoff(self, event_id: str, reason: str, summary: str) -> None:
        assert self.db
        await self.db.execute(
            "INSERT INTO handoffs(event_id, reason, summary, created_at) VALUES (?, ?, ?, ?)",
            (event_id, reason, summary, time.time()),
        )
        await self.db.commit()

    async def ack(self, event_id: str, result: str, note: str) -> None:
        assert self.db
        await self.db.execute(
            "UPDATE events SET status='acked', result=?, note=?, acked_at=? WHERE id=?",
            (result, note[:1000], time.time(), event_id),
        )
        await self.db.commit()

    async def stats(self) -> dict[str, Any]:
        assert self.db
        rows = await (await self.db.execute("SELECT status, COUNT(*) c FROM events GROUP BY status")).fetchall()
        handoffs = await (
            await self.db.execute("SELECT event_id, reason, summary, created_at FROM handoffs ORDER BY id DESC LIMIT 10")
        ).fetchall()
        return {
            "events": {row["status"]: row["c"] for row in rows},
            "recent_handoffs": [dict(row) for row in handoffs],
        }

    async def purge_seen_hooks(self, older_than_seconds: int = 7 * 86400) -> None:
        assert self.db
        await self.db.execute("DELETE FROM seen_hooks WHERE created_at<?", (time.time() - older_than_seconds,))
        await self.db.commit()

"""商户认证档案的本地持久化边界。

所有待办以 ``dedup_key`` 建唯一索引：重复上传、重复扫描、服务重启后再次对账
都只会命中同一条待办，不会产生第二条。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .domain import (
    Dispute,
    Event,
    Inspection,
    MaterialVersion,
    Merchant,
    RemediationTask,
    Review,
    Suspension,
    Todo,
    event_hash,
    now_iso,
)


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        conn = self.connection
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS merchants (
                merchant_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS material_versions (
                version_id TEXT PRIMARY KEY,
                merchant_id TEXT NOT NULL,
                category TEXT NOT NULL,
                version INTEGER NOT NULL,
                content_ref TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                uploaded_by TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_until TEXT NOT NULL,
                status TEXT NOT NULL,
                approved_at TEXT,
                superseded_at TEXT,
                UNIQUE(merchant_id, category, version),
                UNIQUE(merchant_id, category, content_hash)
            );

            CREATE TABLE IF NOT EXISTS reviews (
                review_id TEXT PRIMARY KEY,
                version_id TEXT NOT NULL,
                merchant_id TEXT NOT NULL,
                reviewer_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inspections (
                inspection_id TEXT PRIMARY KEY,
                merchant_id TEXT NOT NULL,
                inspector_id TEXT NOT NULL,
                category TEXT,
                result TEXT NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                voided_at TEXT
            );

            CREATE TABLE IF NOT EXISTS remediation_tasks (
                task_id TEXT PRIMARY KEY,
                merchant_id TEXT NOT NULL,
                inspection_id TEXT NOT NULL UNIQUE,
                category TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                due_at TEXT NOT NULL DEFAULT '',
                resolved_at TEXT,
                resolution_version_id TEXT
            );

            CREATE TABLE IF NOT EXISTS suspensions (
                suspension_id TEXT PRIMARY KEY,
                merchant_id TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                operator_id TEXT NOT NULL,
                suspended_at TEXT NOT NULL,
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS disputes (
                dispute_id TEXT PRIMARY KEY,
                merchant_id TEXT NOT NULL,
                ref_type TEXT NOT NULL,
                ref_id TEXT NOT NULL,
                raised_by TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                reviewer_id TEXT NOT NULL DEFAULT '',
                resolution_comment TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS todos (
                todo_id TEXT PRIMARY KEY,
                merchant_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                dedup_key TEXT NOT NULL UNIQUE,
                reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ref_type TEXT NOT NULL DEFAULT '',
                ref_id TEXT NOT NULL DEFAULT '',
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER NOT NULL,
                merchant_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                action TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                entry_hash TEXT NOT NULL,
                PRIMARY KEY(merchant_id, seq)
            );
            """
        )
        conn.commit()

    # ---- 商户 ----

    def save_merchant(self, merchant: Merchant) -> Merchant:
        value = Merchant(
            merchant.merchant_id, merchant.owner_id, merchant.name,
            merchant.created_at or now_iso(),
        )
        self.connection.execute(
            "INSERT INTO merchants(merchant_id, owner_id, name, created_at) "
            "VALUES(?,?,?,?) ON CONFLICT(merchant_id) DO UPDATE SET "
            "owner_id=excluded.owner_id, name=excluded.name",
            (value.merchant_id, value.owner_id, value.name, value.created_at),
        )
        self.connection.commit()
        return value

    def get_merchant(self, merchant_id: str) -> Merchant | None:
        row = self.connection.execute(
            "SELECT merchant_id, owner_id, name, created_at FROM merchants WHERE merchant_id=?",
            (merchant_id,),
        ).fetchone()
        return Merchant(**dict(row)) if row else None

    def list_merchant_ids(self) -> list[str]:
        return [r[0] for r in self.connection.execute(
            "SELECT merchant_id FROM merchants ORDER BY merchant_id")]

    # ---- 材料版本 ----

    def insert_version(self, item: MaterialVersion) -> MaterialVersion:
        self.connection.execute(
            "INSERT INTO material_versions(version_id, merchant_id, category, version, "
            "content_ref, content_hash, uploaded_by, uploaded_at, valid_from, valid_until, "
            "status, approved_at, superseded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item.version_id, item.merchant_id, item.category, item.version,
             item.content_ref, item.content_hash, item.uploaded_by, item.uploaded_at,
             item.valid_from, item.valid_until, item.status,
             item.approved_at, item.superseded_at),
        )
        self.connection.commit()
        return item

    def find_version_by_hash(self, merchant_id: str, category: str,
                             content_hash: str) -> MaterialVersion | None:
        row = self.connection.execute(
            "SELECT * FROM material_versions WHERE merchant_id=? AND category=? AND content_hash=?",
            (merchant_id, category, content_hash),
        ).fetchone()
        return MaterialVersion(**dict(row)) if row else None

    def get_version(self, version_id: str) -> MaterialVersion | None:
        row = self.connection.execute(
            "SELECT * FROM material_versions WHERE version_id=?", (version_id,)).fetchone()
        return MaterialVersion(**dict(row)) if row else None

    def next_version_number(self, merchant_id: str, category: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM material_versions "
            "WHERE merchant_id=? AND category=?",
            (merchant_id, category),
        ).fetchone()
        return int(row[0]) + 1

    def list_versions(self, merchant_id: str, category: str | None = None) -> list[MaterialVersion]:
        if category is None:
            rows = self.connection.execute(
                "SELECT * FROM material_versions WHERE merchant_id=? ORDER BY category, version",
                (merchant_id,)).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM material_versions WHERE merchant_id=? AND category=? ORDER BY version",
                (merchant_id, category)).fetchall()
        return [MaterialVersion(**dict(r)) for r in rows]

    def approve_version(self, version_id: str, approved_at: str) -> None:
        self.connection.execute(
            "UPDATE material_versions SET status=?, approved_at=? WHERE version_id=?",
            ("approved", approved_at, version_id))
        self.connection.commit()

    def reject_version(self, version_id: str) -> None:
        self.connection.execute(
            "UPDATE material_versions SET status=? WHERE version_id=?",
            ("rejected", version_id))
        self.connection.commit()

    def supersede_versions(self, merchant_id: str, category: str,
                           keep_version_id: str, marked_at: str) -> list[str]:
        """把同分项其余通过版本标记为被替代，返回受影响的 version_id。"""
        rows = self.connection.execute(
            "SELECT version_id FROM material_versions WHERE merchant_id=? AND category=? "
            "AND status='approved' AND version_id<>?",
            (merchant_id, category, keep_version_id)).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            self.connection.execute(
                "UPDATE material_versions SET status='superseded', superseded_at=? "
                "WHERE merchant_id=? AND category=? AND status='approved' AND version_id<>?",
                (marked_at, merchant_id, category, keep_version_id))
            self.connection.commit()
        return ids

    # ---- 评审意见 ----

    def insert_review(self, review: Review) -> Review:
        self.connection.execute(
            "INSERT INTO reviews(review_id, version_id, merchant_id, reviewer_id, "
            "decision, comment, created_at) VALUES(?,?,?,?,?,?,?)",
            (review.review_id, review.version_id, review.merchant_id, review.reviewer_id,
             review.decision, review.comment, review.created_at))
        self.connection.commit()
        return review

    def list_reviews(self, merchant_id: str) -> list[Review]:
        rows = self.connection.execute(
            "SELECT review_id, version_id, merchant_id, reviewer_id, decision, comment, created_at "
            "FROM reviews WHERE merchant_id=? ORDER BY created_at", (merchant_id,)).fetchall()
        return [Review(**dict(r)) for r in rows]

    # ---- 抽查与整改 ----

    def insert_inspection(self, item: Inspection) -> Inspection:
        self.connection.execute(
            "INSERT INTO inspections(inspection_id, merchant_id, inspector_id, category, "
            "result, comment, created_at, voided_at) VALUES(?,?,?,?,?,?,?,?)",
            (item.inspection_id, item.merchant_id, item.inspector_id, item.category,
             item.result, item.comment, item.created_at, item.voided_at))
        self.connection.commit()
        return item

    def get_inspection(self, inspection_id: str) -> Inspection | None:
        row = self.connection.execute(
            "SELECT * FROM inspections WHERE inspection_id=?", (inspection_id,)).fetchone()
        return Inspection(**dict(row)) if row else None

    def list_inspections(self, merchant_id: str) -> list[Inspection]:
        rows = self.connection.execute(
            "SELECT * FROM inspections WHERE merchant_id=? ORDER BY created_at",
            (merchant_id,)).fetchall()
        return [Inspection(**dict(r)) for r in rows]

    def void_inspection(self, inspection_id: str, voided_at: str) -> None:
        self.connection.execute(
            "UPDATE inspections SET voided_at=? WHERE inspection_id=?",
            (voided_at, inspection_id))
        self.connection.commit()

    def insert_remediation(self, item: RemediationTask) -> RemediationTask:
        self.connection.execute(
            "INSERT INTO remediation_tasks(task_id, merchant_id, inspection_id, category, "
            "status, created_at, due_at, resolved_at, resolution_version_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (item.task_id, item.merchant_id, item.inspection_id, item.category,
             item.status, item.created_at, item.due_at, item.resolved_at,
             item.resolution_version_id))
        self.connection.commit()
        return item

    def get_remediation_by_inspection(self, inspection_id: str) -> RemediationTask | None:
        row = self.connection.execute(
            "SELECT * FROM remediation_tasks WHERE inspection_id=?", (inspection_id,)).fetchone()
        return RemediationTask(**dict(row)) if row else None

    def list_remediations(self, merchant_id: str) -> list[RemediationTask]:
        rows = self.connection.execute(
            "SELECT * FROM remediation_tasks WHERE merchant_id=? ORDER BY created_at",
            (merchant_id,)).fetchall()
        return [RemediationTask(**dict(r)) for r in rows]

    def list_open_remediations(self) -> list[RemediationTask]:
        rows = self.connection.execute(
            "SELECT * FROM remediation_tasks WHERE status='open' ORDER BY created_at").fetchall()
        return [RemediationTask(**dict(r)) for r in rows]

    def resolve_remediation(self, task_id: str, resolved_at: str,
                            resolution_version_id: str | None) -> None:
        self.connection.execute(
            "UPDATE remediation_tasks SET status='resolved', resolved_at=?, "
            "resolution_version_id=? WHERE task_id=?",
            (resolved_at, resolution_version_id, task_id))
        self.connection.commit()

    def cancel_remediation(self, task_id: str, resolved_at: str) -> None:
        self.connection.execute(
            "UPDATE remediation_tasks SET status='cancelled', resolved_at=? WHERE task_id=?",
            (resolved_at, task_id))
        self.connection.commit()

    # ---- 暂停 ----

    def insert_suspension(self, item: Suspension) -> Suspension:
        self.connection.execute(
            "INSERT INTO suspensions(suspension_id, merchant_id, reason, operator_id, "
            "suspended_at, resolved_at) VALUES(?,?,?,?,?,?)",
            (item.suspension_id, item.merchant_id, item.reason, item.operator_id,
             item.suspended_at, item.resolved_at))
        self.connection.commit()
        return item

    def list_suspensions(self, merchant_id: str) -> list[Suspension]:
        rows = self.connection.execute(
            "SELECT * FROM suspensions WHERE merchant_id=? ORDER BY suspended_at",
            (merchant_id,)).fetchall()
        return [Suspension(**dict(r)) for r in rows]

    def resolve_suspension(self, suspension_id: str, resolved_at: str) -> None:
        self.connection.execute(
            "UPDATE suspensions SET resolved_at=? WHERE suspension_id=? AND resolved_at IS NULL",
            (resolved_at, suspension_id))
        self.connection.commit()

    # ---- 争议 ----

    def insert_dispute(self, item: Dispute) -> Dispute:
        self.connection.execute(
            "INSERT INTO disputes(dispute_id, merchant_id, ref_type, ref_id, raised_by, "
            "reason, status, created_at, resolved_at, reviewer_id, resolution_comment) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (item.dispute_id, item.merchant_id, item.ref_type, item.ref_id, item.raised_by,
             item.reason, item.status, item.created_at, item.resolved_at,
             item.reviewer_id, item.resolution_comment))
        self.connection.commit()
        return item

    def get_dispute(self, dispute_id: str) -> Dispute | None:
        row = self.connection.execute(
            "SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)).fetchone()
        return Dispute(**dict(row)) if row else None

    def list_disputes(self, merchant_id: str | None = None, status: str | None = None) -> list[Dispute]:
        sql = "SELECT * FROM disputes"
        clauses: list[str] = []
        params: list[str] = []
        if merchant_id:
            clauses.append("merchant_id=?")
            params.append(merchant_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        return [Dispute(**dict(r)) for r in self.connection.execute(sql, params).fetchall()]

    def resolve_dispute(self, dispute_id: str, status: str, reviewer_id: str,
                        comment: str, resolved_at: str) -> None:
        self.connection.execute(
            "UPDATE disputes SET status=?, reviewer_id=?, resolution_comment=?, resolved_at=? "
            "WHERE dispute_id=?",
            (status, reviewer_id, comment, resolved_at, dispute_id))
        self.connection.commit()

    # ---- 待办 ----

    def add_todo(self, todo: Todo) -> tuple[Todo, bool]:
        """插入待办；dedup_key 已存在时返回库中既有记录与 False。"""
        existing = self.get_todo_by_key(todo.dedup_key)
        if existing is not None:
            return existing, False
        self.connection.execute(
            "INSERT INTO todos(todo_id, merchant_id, kind, dedup_key, reason, status, "
            "created_at, ref_type, ref_id, completed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (todo.todo_id, todo.merchant_id, todo.kind, todo.dedup_key, todo.reason,
             todo.status, todo.created_at, todo.ref_type, todo.ref_id, todo.completed_at))
        self.connection.commit()
        return todo, True

    def get_todo_by_key(self, dedup_key: str) -> Todo | None:
        row = self.connection.execute(
            "SELECT * FROM todos WHERE dedup_key=?", (dedup_key,)).fetchone()
        return Todo(**dict(row)) if row else None

    def list_todos(self, merchant_id: str | None = None, status: str | None = None) -> list[Todo]:
        sql = "SELECT * FROM todos"
        clauses: list[str] = []
        params: list[str] = []
        if merchant_id:
            clauses.append("merchant_id=?")
            params.append(merchant_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        return [Todo(**dict(r)) for r in self.connection.execute(sql, params).fetchall()]

    def complete_todo(self, todo_id: str, completed_at: str) -> None:
        self.connection.execute(
            "UPDATE todos SET status='done', completed_at=? WHERE todo_id=?",
            (completed_at, todo_id))
        self.connection.commit()

    def complete_todo_by_key(self, dedup_key: str, completed_at: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE todos SET status='done', completed_at=? WHERE dedup_key=? AND status='open'",
            (completed_at, dedup_key))
        self.connection.commit()
        return cursor.rowcount > 0

    def cancel_todo_by_key(self, dedup_key: str, completed_at: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE todos SET status='cancelled', completed_at=? WHERE dedup_key=? AND status='open'",
            (completed_at, dedup_key))
        self.connection.commit()
        return cursor.rowcount > 0

    # ---- 事件（哈希链凭证） ----

    def append_event(self, merchant_id: str, actor_id: str, action: str,
                     payload: dict, created_at: str) -> Event:
        conn = self.connection
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS last_seq, "
            "(SELECT entry_hash FROM events WHERE merchant_id=? ORDER BY seq DESC LIMIT 1) AS prev "
            "FROM events WHERE merchant_id=?",
            (merchant_id, merchant_id)).fetchone()
        seq = int(row["last_seq"]) + 1
        prev_hash = row["prev"] or ("0" * 64)
        digest = event_hash(seq, merchant_id, actor_id, action, payload, created_at, prev_hash)
        import json as _json
        conn.execute(
            "INSERT INTO events(seq, merchant_id, actor_id, action, payload, created_at, "
            "prev_hash, entry_hash) VALUES(?,?,?,?,?,?,?,?)",
            (seq, merchant_id, actor_id, action,
             _json.dumps(payload, ensure_ascii=False, sort_keys=True),
             created_at, prev_hash, digest))
        conn.commit()
        return Event(seq, merchant_id, actor_id, action, payload, created_at, prev_hash, digest)

    def list_events(self, merchant_id: str) -> list[Event]:
        import json as _json
        rows = self.connection.execute(
            "SELECT * FROM events WHERE merchant_id=? ORDER BY seq", (merchant_id,)).fetchall()
        return [
            Event(r["seq"], r["merchant_id"], r["actor_id"], r["action"],
                  _json.loads(r["payload"]), r["created_at"], r["prev_hash"], r["entry_hash"])
            for r in rows
        ]

    def close(self) -> None:
        self.connection.close()

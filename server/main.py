import os
import sqlite3
import secrets
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator


APP_NAME = "ValeSync"
DB_PATH = Path(os.getenv("DB_PATH", "./data/valesync.db"))
API_TOKEN = os.getenv("VALESYNC_API_TOKEN", "")

app = FastAPI(
    title=APP_NAME,
    version="0.2.0",
    description="ValeSync server and local-agent task bridge.",
)


# ============================================================
# MODELS
# ============================================================

class CreateFileTask(BaseModel):
    action: Literal["create_file"]
    path: str = Field(min_length=1, max_length=400)
    content: str = Field(max_length=2_000_000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class CreateFileRequest(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    content: str = Field(max_length=2_000_000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class TaskResult(BaseModel):
    status: Literal["completed", "failed"]
    message: str = Field(min_length=1, max_length=4000)


# ============================================================
# VALIDATION
# ============================================================

def validate_relative_path(value: str) -> str:
    if "\x00" in value:
        raise ValueError("NUL byte is not allowed")

    normalized = value.replace("\\", "/")

    if normalized.startswith("/"):
        raise ValueError("Absolute paths are not allowed")

    if len(normalized) >= 2 and normalized[1] == ":":
        raise ValueError("Absolute paths are not allowed")

    parts = [
        part
        for part in normalized.split("/")
        if part not in ("", ".")
    ]

    if any(part == ".." for part in parts):
        raise ValueError("Path traversal is not allowed")

    if not parts:
        raise ValueError("Path cannot be empty")

    return "/".join(parts)


# ============================================================
# DATABASE
# ============================================================

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    conn = sqlite3.connect(
        DB_PATH,
        timeout=10,
        isolation_level=None,
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")

    return conn


def init_db() -> None:
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                path TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK(
                        status IN (
                            'pending',
                            'processing',
                            'completed',
                            'failed'
                        )
                    ),
                created_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,
                result_message TEXT
            )
            """
        )

        db.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_tasks_status_created
            ON tasks(status, created_at)
            """
        )


# ============================================================
# AUTH
# ============================================================

def require_token(
    authorization: str | None = Header(default=None),
) -> None:

    if not API_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="Server API token is not configured",
        )

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization format",
        )

    supplied = authorization.removeprefix("Bearer ").strip()

    if not secrets.compare_digest(
        supplied,
        API_TOKEN,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
def startup() -> None:
    init_db()


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": APP_NAME,
        "version": "0.2.0",
    }


# ============================================================
# SIMPLE CREATE-FILE ENDPOINT
# ============================================================

@app.post("/create-file")
def create_file(
    request: CreateFileRequest,
    _: None = Depends(require_token),
) -> dict:

    task_id = secrets.token_urlsafe(18)

    with get_db() as db:
        db.execute(
            """
            INSERT INTO tasks (
                id,
                action,
                path,
                content,
                status
            )
            VALUES (
                ?,
                'create_file',
                ?,
                ?,
                'pending'
            )
            """,
            (
                task_id,
                request.path,
                request.content,
            ),
        )

    return {
        "task_id": task_id,
        "status": "pending",
        "action": "create_file",
        "path": request.path,
    }


# ============================================================
# ORIGINAL API
# ============================================================

@app.post("/api/tasks")
def create_task(
    task: CreateFileTask,
    _: None = Depends(require_token),
) -> dict:

    task_id = secrets.token_urlsafe(18)

    with get_db() as db:
        db.execute(
            """
            INSERT INTO tasks (
                id,
                action,
                path,
                content,
                status
            )
            VALUES (
                ?,
                ?,
                ?,
                ?,
                'pending'
            )
            """,
            (
                task_id,
                task.action,
                task.path,
                task.content,
            ),
        )

    return {
        "task_id": task_id,
        "status": "pending",
    }


# ============================================================
# AGENT: GET NEXT TASK
# ============================================================

@app.get("/api/tasks/next")
def get_next_task(
    _: None = Depends(require_token),
) -> dict:

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")

        row = db.execute(
            """
            SELECT
                id,
                action,
                path,
                content
            FROM tasks
            WHERE status = 'pending'
            ORDER BY created_at, rowid
            LIMIT 1
            """
        ).fetchone()

        if row is None:
            db.execute("COMMIT")
            return {
                "task": None,
            }

        db.execute(
            """
            UPDATE tasks
            SET
                status = 'processing',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'pending'
            """,
            (row["id"],),
        )

        db.execute("COMMIT")

    return {
        "task": {
            "id": row["id"],
            "action": row["action"],
            "path": row["path"],
            "content": row["content"],
        }
    }


# ============================================================
# AGENT: REPORT RESULT
# ============================================================

@app.post("/api/tasks/{task_id}/result")
def report_result(
    task_id: str,
    result: TaskResult,
    _: None = Depends(require_token),
) -> dict:

    with get_db() as db:
        cur = db.execute(
            """
            UPDATE tasks
            SET
                status = ?,
                result_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'processing'
            """,
            (
                result.status,
                result.message,
                task_id,
            ),
        )

        if cur.rowcount != 1:
            raise HTTPException(
                status_code=404,
                detail="Task not found or not processing",
            )

    return {
        "task_id": task_id,
        "status": result.status,
    }


# ============================================================
# GET TASK
# ============================================================

@app.get("/api/tasks/{task_id}")
def get_task(
    task_id: str,
    _: None = Depends(require_token),
) -> dict:

    with get_db() as db:
        row = db.execute(
            """
            SELECT
                id,
                action,
                path,
                status,
                created_at,
                updated_at,
                result_message
            FROM tasks
            WHERE id = ?
            """,
            (task_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    return dict(row)
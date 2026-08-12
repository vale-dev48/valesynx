import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator


APP_NAME = "ValeSync"
APP_VERSION = "0.4.0"

DB_PATH = Path(
    os.getenv("DB_PATH", "./data/valesync.db")
)

API_TOKEN = os.getenv(
    "VALESYNC_API_TOKEN",
    "",
)

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description="ValeSync server and local-agent task bridge.",
)


# ============================================================
# VALIDATION
# ============================================================

def validate_relative_path(value: str) -> str:
    if "\x00" in value:
        raise ValueError("NUL byte is not allowed")

    normalized = value.replace("\\", "/").strip()

    if not normalized:
        raise ValueError("Path cannot be empty")

    if normalized == ".":
        return "."

    if normalized.startswith("/"):
        raise ValueError("Absolute paths are not allowed")

    if len(normalized) >= 2 and normalized[1] == ":":
        raise ValueError("Absolute paths are not allowed")

    parts = [
        part
        for part in normalized.split("/")
        if part not in ("", ".")
    ]

    if not parts:
        raise ValueError("Path cannot be empty")

    if any(part == ".." for part in parts):
        raise ValueError("Path traversal is not allowed")

    return "/".join(parts)


# ============================================================
# MODELS
# ============================================================

Action = Literal[
    "create_file",
    "update_file",
    "create_folder",
    "delete",
    "move",
    "list_files",
    "read_file",
]


class TaskCreate(BaseModel):
    action: Action

    path: str = Field(
        min_length=1,
        max_length=400,
    )

    content: str | None = Field(
        default=None,
        max_length=2_000_000,
    )

    destination: str | None = Field(
        default=None,
        max_length=400,
    )

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("destination")
    @classmethod
    def validate_destination(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None

        return validate_relative_path(value)


class CreateFileRequest(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=400,
    )

    content: str = Field(
        max_length=2_000_000,
    )

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class UpdateFileRequest(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=400,
    )

    content: str = Field(
        max_length=2_000_000,
    )

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class CreateFolderRequest(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=400,
    )

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class DeleteRequest(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=400,
    )

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class MoveRequest(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=400,
    )

    destination: str = Field(
        min_length=1,
        max_length=400,
    )

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("destination")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        return validate_relative_path(value)


class ReadFileRequest(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=400,
    )

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class ListFilesRequest(BaseModel):
    path: str = Field(
        default=".",
        max_length=400,
    )

    recursive: bool = False

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if value.strip() in ("", "."):
            return "."

        return validate_relative_path(value)


class TaskResult(BaseModel):
    status: Literal[
        "completed",
        "failed",
    ]

    message: str = Field(
        min_length=1,
        max_length=4000,
    )

    data: dict | list | None = None


# ============================================================
# DATABASE
# ============================================================

def init_db() -> None:
    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                path TEXT NOT NULL,
                content TEXT NOT NULL,
                destination TEXT,
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
                result_message TEXT,
                result_data TEXT
            )
            """
        )

        columns = {
            row[1]
            for row in db.execute(
                "PRAGMA table_info(tasks)"
            ).fetchall()
        }

        if "destination" not in columns:
            db.execute(
                """
                ALTER TABLE tasks
                ADD COLUMN destination TEXT
                """
            )

        if "result_message" not in columns:
            db.execute(
                """
                ALTER TABLE tasks
                ADD COLUMN result_message TEXT
                """
            )

        if "result_data" not in columns:
            db.execute(
                """
                ALTER TABLE tasks
                ADD COLUMN result_data TEXT
                """
            )

        db.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_tasks_status_created
            ON tasks(status, created_at)
            """
        )

        db.commit()


@contextmanager
def get_db():
    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    db = sqlite3.connect(
        DB_PATH,
        timeout=10,
    )

    db.row_factory = sqlite3.Row

    db.execute(
        "PRAGMA busy_timeout=10000"
    )

    try:
        yield db
        db.commit()

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


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

    supplied = authorization.removeprefix(
        "Bearer "
    ).strip()

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
        "version": APP_VERSION,
    }


# ============================================================
# INSERT TASK
# ============================================================

def insert_task(
    action: str,
    path: str,
    content: str | None = None,
    destination: str | None = None,
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
                destination,
                status
            )
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (
                task_id,
                action,
                path,
                content or "",
                destination or "",
            ),
        )

    return {
        "task_id": task_id,
        "status": "pending",
        "action": action,
        "path": path,
    }


# ============================================================
# CREATE FILE
# ============================================================

@app.post("/create-file")
def create_file(
    request: CreateFileRequest,
    _: None = Depends(require_token),
) -> dict:

    return insert_task(
        action="create_file",
        path=request.path,
        content=request.content,
    )


# ============================================================
# UPDATE FILE
# ============================================================

@app.post("/update-file")
def update_file(
    request: UpdateFileRequest,
    _: None = Depends(require_token),
) -> dict:

    return insert_task(
        action="update_file",
        path=request.path,
        content=request.content,
    )


# ============================================================
# CREATE FOLDER
# ============================================================

@app.post("/create-folder")
def create_folder(
    request: CreateFolderRequest,
    _: None = Depends(require_token),
) -> dict:

    return insert_task(
        action="create_folder",
        path=request.path,
    )


# ============================================================
# DELETE
# ============================================================

@app.post("/delete")
def delete(
    request: DeleteRequest,
    _: None = Depends(require_token),
) -> dict:

    return insert_task(
        action="delete",
        path=request.path,
    )


# ============================================================
# MOVE
# ============================================================

@app.post("/move")
def move(
    request: MoveRequest,
    _: None = Depends(require_token),
) -> dict:

    return insert_task(
        action="move",
        path=request.path,
        destination=request.destination,
    )


# ============================================================
# READ FILE
# ============================================================

@app.post("/read-file")
def read_file(
    request: ReadFileRequest,
    _: None = Depends(require_token),
) -> dict:

    return insert_task(
        action="read_file",
        path=request.path,
    )


# ============================================================
# LIST FILES
# ============================================================

@app.post("/list-files")
def list_files(
    request: ListFilesRequest,
    _: None = Depends(require_token),
) -> dict:

    return insert_task(
        action="list_files",
        path=request.path,
        content=(
            "recursive"
            if request.recursive
            else ""
        ),
    )


# ============================================================
# GENERIC TASK API
# ============================================================

@app.post("/api/tasks")
def create_task(
    task: TaskCreate,
    _: None = Depends(require_token),
) -> dict:

    return insert_task(
        action=task.action,
        path=task.path,
        content=task.content,
        destination=task.destination,
    )


# ============================================================
# GET NEXT TASK
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
                content,
                destination
            FROM tasks
            WHERE status = 'pending'
            ORDER BY created_at, rowid
            LIMIT 1
            """
        ).fetchone()

        if row is None:
            db.execute("COMMIT")

            return {
                "task": None
            }

        updated = db.execute(
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

        if updated.rowcount != 1:
            db.execute("ROLLBACK")

            return {
                "task": None
            }

        db.execute("COMMIT")

        return {
            "task": {
                "id": row["id"],
                "action": row["action"],
                "path": row["path"],
                "content": row["content"],
                "destination": row["destination"],
            }
        }


# ============================================================
# REPORT RESULT
# ============================================================

@app.post("/api/tasks/{task_id}/result")
def report_result(
    task_id: str,
    result: TaskResult,
    _: None = Depends(require_token),
) -> dict:

    result_data = (
        json.dumps(
            result.data,
            ensure_ascii=False,
        )
        if result.data is not None
        else None
    )

    with get_db() as db:

        cur = db.execute(
            """
            UPDATE tasks
            SET
                status = ?,
                result_message = ?,
                result_data = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'processing'
            """,
            (
                result.status,
                result.message,
                result_data,
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
                destination,
                status,
                created_at,
                updated_at,
                result_message,
                result_data
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

    result = dict(row)

    if result.get("result_data"):
        try:
            result["result_data"] = json.loads(
                result["result_data"]
            )
        except json.JSONDecodeError:
            pass

    return result
import json
import os
import time
from pathlib import Path

import requests


SERVER_URL = os.getenv(
    "VALESYNC_SERVER_URL",
    "http://127.0.0.1:8000",
).rstrip("/")

TOKEN = os.getenv(
    "VALESYNC_API_TOKEN",
    "",
)

WORKSPACE = (
    Path(
        os.getenv(
            "VALESYNC_WORKSPACE",
            Path.home() / "ValeWorkspace",
        )
    )
    .expanduser()
    .resolve()
)

POLL_SECONDS = max(
    1.0,
    float(
        os.getenv(
            "VALESYNC_POLL_SECONDS",
            "3",
        )
    ),
)

TIMEOUT = 15

MAX_READ_SIZE = 2_000_000

session = requests.Session()

session.headers.update(
    {
        "Authorization": f"Bearer {TOKEN}",
    }
)


# ============================================================
# SAFE PATH
# ============================================================

def safe_target(
    relative_path: str,
) -> Path:

    if not isinstance(
        relative_path,
        str,
    ):
        raise ValueError(
            "Invalid path"
        )

    if "\x00" in relative_path:
        raise ValueError(
            "NUL byte is not allowed"
        )

    normalized = relative_path.replace(
        "\\",
        "/",
    ).strip()

    if normalized in ("", "."):
        return WORKSPACE

    if normalized.startswith("/"):
        raise ValueError(
            "Absolute paths are not allowed"
        )

    if len(normalized) >= 2 and normalized[1] == ":":
        raise ValueError(
            "Absolute paths are not allowed"
        )

    candidate = (
        WORKSPACE / normalized
    ).resolve()

    try:
        candidate.relative_to(
            WORKSPACE
        )
    except ValueError as exc:
        raise ValueError(
            "Path escapes workspace"
        ) from exc

    return candidate


# ============================================================
# CONFIRMATION
# ============================================================

def ask_confirmation(
    action: str,
    path: str,
    content: str | None = None,
    destination: str | None = None,
) -> bool:

    print()

    if action == "create_file":
        print(
            f"Create file: {path}"
        )

    elif action == "update_file":
        print(
            f"Modify file: {path}"
        )

    elif action == "create_folder":
        print(
            f"Create folder: {path}"
        )

    elif action == "delete":
        print(
            f"Delete: {path}"
        )

    elif action == "move":
        print(
            f"Move: {path} -> {destination}"
        )

    print(
        f"Workspace: {WORKSPACE}"
    )

    if content is not None:
        print(
            f"Size: {len(content):,} characters"
        )

    while True:

        answer = input(
            "Continue? [S/N]: "
        ).strip().lower()

        if answer in (
            "s",
            "si",
            "sì",
            "y",
            "yes",
        ):
            return True

        if answer in (
            "n",
            "no",
        ):
            return False

        print(
            "Use S or N."
        )


# ============================================================
# CREATE FILE
# ============================================================

def create_file(
    path: str,
    content: str,
) -> str:

    target = safe_target(path)

    if target.exists():
        raise FileExistsError(
            f"File already exists: {path}"
        )

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    target.write_text(
        content,
        encoding="utf-8",
        newline="",
    )

    return (
        f"Created {path}"
    )


# ============================================================
# UPDATE FILE
# ============================================================

def update_file(
    path: str,
    content: str,
) -> str:

    target = safe_target(path)

    if not target.exists():
        raise FileNotFoundError(
            f"File does not exist: {path}"
        )

    if not target.is_file():
        raise IsADirectoryError(
            f"Not a file: {path}"
        )

    target.write_text(
        content,
        encoding="utf-8",
        newline="",
    )

    return (
        f"Updated {path}"
    )


# ============================================================
# CREATE FOLDER
# ============================================================

def create_folder(
    path: str,
) -> str:

    target = safe_target(path)

    if target.exists():
        raise FileExistsError(
            f"Already exists: {path}"
        )

    target.mkdir(
        parents=True,
        exist_ok=False,
    )

    return (
        f"Created folder {path}"
    )


# ============================================================
# DELETE
# ============================================================

def delete_path(
    path: str,
) -> str:

    target = safe_target(path)

    if target == WORKSPACE:
        raise PermissionError(
            "Cannot delete workspace"
        )

    if not target.exists():
        raise FileNotFoundError(
            f"Does not exist: {path}"
        )

    if target.is_file():
        target.unlink()

        return (
            f"Deleted {path}"
        )

    if target.is_dir():

        try:
            target.rmdir()

        except OSError as exc:
            raise OSError(
                "Folder is not empty"
            ) from exc

        return (
            f"Deleted folder {path}"
        )

    raise OSError(
        f"Unsupported filesystem object: {path}"
    )


# ============================================================
# MOVE
# ============================================================

def move_path(
    path: str,
    destination: str,
) -> str:

    source = safe_target(path)

    target = safe_target(
        destination
    )

    if source == WORKSPACE:
        raise PermissionError(
            "Cannot move workspace"
        )

    if not source.exists():
        raise FileNotFoundError(
            f"Source does not exist: {path}"
        )

    if target.exists():
        raise FileExistsError(
            f"Destination already exists: {destination}"
        )

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    source.rename(
        target
    )

    return (
        f"Moved {path} -> {destination}"
    )


# ============================================================
# READ FILE
# ============================================================

def read_file(
    path: str,
) -> tuple[str, dict]:

    target = safe_target(path)

    if not target.exists():
        raise FileNotFoundError(
            f"File does not exist: {path}"
        )

    if not target.is_file():
        raise IsADirectoryError(
            f"Not a file: {path}"
        )

    size_bytes = target.stat().st_size

    if size_bytes > MAX_READ_SIZE:
        raise ValueError(
            f"File too large: {size_bytes:,} bytes"
        )

    try:
        content = target.read_text(
            encoding="utf-8"
        )

    except UnicodeDecodeError as exc:
        raise ValueError(
            "File is not valid UTF-8 text"
        ) from exc

    data = {
        "path": path,
        "size": len(content),
        "content": content,
    }

    return (
        f"Read {path}",
        data,
    )


# ============================================================
# LIST FILES
# ============================================================

def list_files(
    path: str,
    recursive: bool,
) -> tuple[str, list]:

    target = safe_target(path)

    if not target.exists():
        raise FileNotFoundError(
            f"Directory does not exist: {path}"
        )

    if not target.is_dir():
        raise NotADirectoryError(
            f"Not a directory: {path}"
        )

    iterator = (
        target.rglob("*")
        if recursive
        else target.iterdir()
    )

    results = []

    for item in iterator:

        relative = item.relative_to(
            WORKSPACE
        )

        results.append(
            {
                "path": relative.as_posix(),
                "type": (
                    "directory"
                    if item.is_dir()
                    else "file"
                ),
            }
        )

    results.sort(
        key=lambda item: (
            item["type"] != "directory",
            item["path"].lower(),
        )
    )

    return (
        f"Listed {path}",
        results,
    )


# ============================================================
# PROCESS TASK
# ============================================================

def process_task(
    task: dict,
) -> tuple[str, str, dict | list | None]:

    action = task.get("action")
    path = task.get("path")
    content = task.get("content")
    destination = task.get("destination")

    if not action:
        return (
            "failed",
            "Missing action",
            None,
        )

    if not path:
        return (
            "failed",
            "Missing path",
            None,
        )

    try:

        # ----------------------------------------------------
        # READ
        # ----------------------------------------------------

        if action == "read_file":

            message, data = read_file(
                path
            )

            return (
                "completed",
                message,
                data,
            )

        # ----------------------------------------------------
        # LIST
        # ----------------------------------------------------

        if action == "list_files":

            recursive = (
                content == "recursive"
            )

            message, data = list_files(
                path,
                recursive,
            )

            return (
                "completed",
                message,
                data,
            )

        # ----------------------------------------------------
        # WRITE / MODIFY
        # ----------------------------------------------------

        if not ask_confirmation(
            action,
            path,
            content,
            destination,
        ):

            return (
                "failed",
                "Operation rejected by user",
                None,
            )

        if action == "create_file":

            if not isinstance(
                content,
                str,
            ):
                return (
                    "failed",
                    "Content is missing",
                    None,
                )

            message = create_file(
                path,
                content,
            )

        elif action == "update_file":

            if not isinstance(
                content,
                str,
            ):
                return (
                    "failed",
                    "Content is missing",
                    None,
                )

            message = update_file(
                path,
                content,
            )

        elif action == "create_folder":

            message = create_folder(
                path
            )

        elif action == "delete":

            message = delete_path(
                path
            )

        elif action == "move":

            if not destination:
                return (
                    "failed",
                    "Destination is missing",
                    None,
                )

            message = move_path(
                path,
                destination,
            )

        else:

            return (
                "failed",
                f"Unsupported action: {action}",
                None,
            )

        return (
            "completed",
            message,
            None,
        )

    except Exception as exc:

        return (
            "failed",
            f"{type(exc).__name__}: {exc}",
            None,
        )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    if not TOKEN:
        raise SystemExit(
            "VALESYNC_API_TOKEN is not set"
        )

    WORKSPACE.mkdir(
        parents=True,
        exist_ok=True,
    )

    print(
        f"ValeSync Agent workspace: {WORKSPACE}"
    )

    print(
        f"ValeSync server: {SERVER_URL}"
    )

    print(
        "Waiting for tasks..."
    )

    while True:

        try:

            response = session.get(
                f"{SERVER_URL}/api/tasks/next",
                timeout=TIMEOUT,
            )

            response.raise_for_status()

            payload = response.json()

            task = payload.get("task")

            if not task:

                time.sleep(
                    POLL_SECONDS
                )

                continue

            task_id = task["id"]

            (
                result_status,
                message,
                data,
            ) = process_task(task)

            result = session.post(
                f"{SERVER_URL}/api/tasks/{task_id}/result",
                json={
                    "status": result_status,
                    "message": message,
                    "data": data,
                },
                timeout=TIMEOUT,
            )

            result.raise_for_status()

            print()

            print(
                json.dumps(
                    {
                        "task_id": task_id,
                        "status": result_status,
                        "message": message,
                    },
                    ensure_ascii=False,
                )
            )

            print()

        except requests.RequestException as exc:

            print(
                f"Server unavailable: {exc}"
            )

            time.sleep(
                min(
                    POLL_SECONDS * 2,
                    15,
                )
            )

        except KeyboardInterrupt:

            print()

            print(
                "Stopping ValeSync Agent"
            )

            break


if __name__ == "__main__":
    main()
import json
import os
import time
from pathlib import Path

import requests

SERVER_URL = os.getenv("VALESYNC_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.getenv("VALESYNC_API_TOKEN", "")
WORKSPACE = Path(os.getenv("VALESYNC_WORKSPACE", Path.home() / "ValeWorkspace")).expanduser().resolve()
POLL_SECONDS = max(1.0, float(os.getenv("VALESYNC_POLL_SECONDS", "3")))
TIMEOUT = 15

session = requests.Session()
session.headers.update({"Authorization": f"Bearer {TOKEN}"})


def safe_target(relative_path: str) -> Path:
    if "\x00" in relative_path:
        raise ValueError("NUL byte is not allowed")
    candidate = (WORKSPACE / relative_path.replace("\\", "/")).resolve()
    try:
        candidate.relative_to(WORKSPACE)
    except ValueError as exc:
        raise ValueError("Path escapes workspace") from exc
    return candidate


def process(task: dict) -> tuple[str, str]:
    if task.get("action") != "create_file":
        return "failed", "Unsupported action"

    target = safe_target(task["path"])
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        return "failed", f"File already exists: {task['path']}"

    content = task.get("content", "")
    if not isinstance(content, str):
        return "failed", "Content must be a string"

    target.write_text(content, encoding="utf-8", newline="")
    return "completed", f"Created {target.relative_to(WORKSPACE)}"


def main() -> None:
    if not TOKEN:
        raise SystemExit("VALESYNC_API_TOKEN is not set")

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    print(f"ValeSync Agent workspace: {WORKSPACE}")
    print(f"ValeSync server: {SERVER_URL}")

    while True:
        try:
            response = session.get(f"{SERVER_URL}/api/tasks/next", timeout=TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            task = payload.get("task")
            if task:
                task_id = task["id"]
                try:
                    result_status, message = process(task)
                except Exception as exc:
                    result_status, message = "failed", f"{type(exc).__name__}: {exc}"
                session.post(
                    f"{SERVER_URL}/api/tasks/{task_id}/result",
                    json={"status": result_status, "message": message},
                    timeout=TIMEOUT,
                ).raise_for_status()
                print(json.dumps({"task_id": task_id, "status": result_status, "message": message}, ensure_ascii=False))
            else:
                time.sleep(POLL_SECONDS)
        except requests.RequestException as exc:
            print(f"Server unavailable: {exc}")
            time.sleep(min(POLL_SECONDS * 2, 15))
        except KeyboardInterrupt:
            print("Stopping ValeSync Agent")
            break


if __name__ == "__main__":
    main()

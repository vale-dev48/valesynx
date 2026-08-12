# ValeSync V1

Safe bridge between a ChatGPT Action / HTTP client and a local Windows agent.

## What V1 does

The only supported operation is `create_file`.

The local agent can write **only** inside `%USERPROFILE%\\ValeWorkspace` (or the path set by `VALESYNC_WORKSPACE`). It does not execute CMD, PowerShell, shell commands, or arbitrary programs.

## Local test

### 1. Server

```powershell
cd ValeSync\server
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:VALESYNC_API_TOKEN="change-this-to-a-long-random-secret"
$env:DB_PATH="$PWD\data\valesync.db"
uvicorn main:app --host 127.0.0.1 --port 8000
```

### 2. Agent (second terminal)

```powershell
cd ValeSync\agent
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:VALESYNC_SERVER_URL="http://127.0.0.1:8000"
$env:VALESYNC_API_TOKEN="change-this-to-a-long-random-secret"
$env:VALESYNC_WORKSPACE="$env:USERPROFILE\ValeWorkspace"
python agent.py
```

### 3. Queue a test file

In a third terminal:

```powershell
$headers = @{ Authorization = "Bearer change-this-to-a-long-random-secret" }
$body = @{
  action = "create_file"
  path = "hello.py"
  content = "print('ciao Vale')"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/tasks" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

The agent should create:

`%USERPROFILE%\ValeWorkspace\hello.py`

## Railway

Deploy the `server` directory as a Railway service using the included Dockerfile.

Set:

- `VALESYNC_API_TOKEN` = a long random secret
- `DB_PATH` = `/data/valesync.db`

Mount a Railway Volume at `/data` so SQLite persists across redeploys.

## ChatGPT Action

After deployment, replace `https://YOUR-RAILWAY-DOMAIN` in `openapi/openapi.yaml` with your Railway public HTTPS domain and import the schema into a GPT Action. Configure API-key authentication using the same bearer token.

V1 intentionally exposes only file creation and task status. Do not add arbitrary command execution to this bridge.

# Git Gallery Runner

A local dashboard for GitHub/GitLab repositories that include Docker Compose files.

## Requirements

- Python 3.10+
- Git
- Docker Desktop or Docker Engine
- Docker Compose v2 (`docker compose`)

## First-time setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run as background service (recommended)

Double-click or run:

```powershell
start.bat
```

Other commands:

```powershell
stop.bat
status.bat
python runner.py restart
```

Dashboard: `http://localhost:8080`

Logs: `data/runner.log`

## Manual run (development)

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn app:app --host 127.0.0.1 --port 8080 --reload
```

## Usage

1. Save GitHub credentials for private repos (optional, global or per project).
2. Save GitLab credentials in the dashboard (optional).
3. Add a repo URL with frontend/backend ports and optional branch.
4. Click **Run / Build** — clones/pulls, auto-generates Docker stack if missing, then builds.
5. Click **Pull + Rebuild** for full cleanup + fresh build.
6. Change ports, click **Restart** to stop and start on new ports.
7. Click **باز کردن** to open `http://localhost:{port}`.
8. Click **Push GitLab** to push the cloned repo to GitLab.

## Auto-dockerize

If a repo has no `docker-compose.yml`, Runner detects the project type and generates:

- `Dockerfile` + `docker-compose.yml` for Node/Next.js, Python, or static sites
- Multi-service stack for `frontend/` + `backend/` layouts (with Postgres if Prisma exists)

## Build a single EXE (optional)

```powershell
pip install pyinstaller
pyinstaller --onefile --name GitGalleryRunner runner.py
```

Then use:

```powershell
.\dist\GitGalleryRunner.exe start
.\dist\GitGalleryRunner.exe stop
```

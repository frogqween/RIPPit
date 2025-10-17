# WARP.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

Project overview
- Local downloader UI built with FastAPI (backend) and a static frontend (HTML/CSS/JS). Downloads are performed via yt-dlp and post-processed with FFmpeg.

Prerequisites
- Python 3.11+
- FFmpeg available on PATH (yt-dlp uses it for muxing/transcoding)

Common commands
- Create/activate venv and install deps (Windows PowerShell):
```bash path=null start=null
py -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -r requirements.txt
```
- Create/activate venv and install deps (macOS/Linux):
```bash path=null start=null
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```
- Run the dev server (hot-reload):
```bash path=null start=null
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload
```
- Open the UI: http://localhost:8000
- Update yt-dlp (when sites change):
```bash path=null start=null
python -m pip install -U yt-dlp
```
- Linting/tests: no linter or test suite is configured in this repo.

High-level architecture
- Backend (FastAPI) — backend/app.py
  - Mounts static frontend at /static from frontend/static.
  - Startup: ensures data/temp/download dirs, initializes SQLite, starts async workers.
  - Endpoints:
    - GET / — serves index.html (expects frontend/static/index.html to exist).
    - GET /api/health — basic health check.
    - GET /api/download-dir — returns resolved download directory.
    - GET /api/probe?url= — lightweight metadata/playlist probe via yt-dlp.
    - POST /api/download — enqueue a download job; body matches backend/models.py::DownloadRequest.
    - GET /api/progress/{job_id} — server-sent events stream for live progress.
    - GET /api/history — recent jobs from SQLite.
    - POST /api/cancel/{job_id} — request cancellation for a running job.
    - POST /api/open-folder — open a directory in the OS file explorer.
- Download pipeline — backend/downloader.py
  - DownloadManager holds an asyncio.Queue of Job items and N workers (settings.MAX_CONCURRENT).
  - new_job(...) builds a Job; enqueue() persists initial state and notifies SSE; workers run _process().
  - _process():
    - Builds yt-dlp options and per-mode format/postprocessor strategy.
    - Probes/uses browser cookies automatically (settings.AUTO_COOKIES) and supports explicit cookie files.
    - Handles playlists: optional playlist_items or selected_urls, maintains per-item and overall progress.
    - Publishes progress via SSE hooks; updates SQLite during download/postprocessing; marks done/cancelled/error.
  - Output naming is flat (no nested folders): %(title)s.%(ext)s in the resolved download dir.
- Persistence — backend/db.py
  - SQLite DB at data/history.db; single table downloads with status, timing, and file metadata.
  - init_db(), insert_job(), update_job(), history() helpers used by app and downloader.
- Data/config — backend/settings.py
  - Directories: BASE_DIR (repo root), DATA_DIR (data/), TEMP_DIR (data/temp/), DB_FILE (data/history.db).
  - DOWNLOAD_DIR defaults to the OS Downloads folder.
  - MAX_CONCURRENT controls worker count; SSE_HEARTBEAT_SEC configures progress stream heartbeat.
  - AUTO_COOKIES and COOKIES_BROWSERS influence cookie selection; FFMPEG binary is resolved from PATH or ./bin/.
- Frontend (static) — frontend/static/
  - index.html + main.js + styles.css. The UI posts to /api/download, subscribes to /api/progress via EventSource, and renders per-item + playlist progress. Optional playlist panel is populated from /api/probe.

Important behavior and notes
- Frontend must exist at frontend/static; otherwise GET / returns 500 ("Frontend not found").
- Overwrite behavior: downloader enables overwrites and does not use yt-dlp's archive; repeated downloads will overwrite by default.
- Cookies: for YouTube URLs, the backend often selects a browser/profile automatically to bypass prompts; explicit cookie_file in the request takes precedence.
- Default download directory is chosen per-OS at runtime and can be fetched via GET /api/download-dir.

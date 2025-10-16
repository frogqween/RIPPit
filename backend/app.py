from __future__ import annotations

import asyncio
import os
import platform
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .settings import settings
from . import db
from .downloader import manager, new_job
from .models import DownloadRequest, DownloadResponse

app = FastAPI(title=settings.APP_NAME)

# Static frontend
static_dir = settings.STATIC_DIR
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
async def _startup():
    # Ensure directories
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    settings.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Init DB
    db.init_db()
    # Start workers
    await manager.start()
    # Touch health log line so we know startup ran
    try:
        from logging import getLogger
        getLogger("rippit.app").info("Startup complete; workers=%s", len(manager.workers))
    except Exception:
        pass


@app.get("/")
async def index():
    index_path = settings.STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(500, detail="Frontend not found")
    return FileResponse(str(index_path))


@app.get("/favicon.ico")
async def favicon():
    # Return empty favicon to stop 404 errors
    return Response(content=b"", media_type="image/x-icon")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/download-dir")
async def get_download_dir():
    return {"path": str(settings.DOWNLOAD_DIR)}


@app.get("/api/probe")
async def probe(url: str):
    # Lightweight extractor to list playlist entries
    import yt_dlp as ytdlp
    
    # For SoundCloud, always use full extraction to get titles
    if "soundcloud" in url.lower():
        opts = {
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "ignoreconfig": True,
            "playlistend": 100,  # Limit to first 100 for reasonable speed
        }
    else:
        # Other sites can use flat extraction
        opts = {
            "skip_download": True,
            "extract_flat": "in_playlist",
            "quiet": True,
            "no_warnings": True,
            "ignoreconfig": True,
        }
    
    with ytdlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    
    if info and info.get("_type") == "playlist":
        entries = info.get("entries") or []
        out = []
        
        for idx, e in enumerate(entries, 1):
            if not e:
                continue
            # Handle both flat and full extraction formats
            pi = e.get("playlist_index") or e.get("__x_forwarded_index") or idx
            
            # Get the best available title
            title = e.get("title")
            if not title and isinstance(e, dict):
                # Try alternate title fields
                title = e.get("fulltitle") or e.get("alt_title")
            if not title:
                title = f"Track {idx}"
            
            # Get the best available URL
            url_entry = e.get("webpage_url") or e.get("url") or e.get("original_url")
            if not url_entry and e.get("id"):
                # Construct SoundCloud URL if we have ID
                if "soundcloud" in url.lower():
                    url_entry = f"https://soundcloud.com/{e.get('uploader', '')}/{e.get('id', '')}"
            
            out.append({
                "index": pi,
                "id": e.get("id") or str(idx),
                "title": title,
                "duration": e.get("duration"),
                "uploader": e.get("uploader") or e.get("channel") or e.get("artist"),
                "url": url_entry,
                "ie_key": e.get("ie_key") or e.get("extractor_key") or e.get("extractor"),
            })
        return {"type": "playlist", "title": info.get("title"), "count": len(out), "entries": out}
    else:
        return {"type": "video", "title": info.get("title"), "id": info.get("id")}


@app.post("/api/download", response_model=DownloadResponse)
async def enqueue_download(req: DownloadRequest):
    # Normalize containers and infer mode if missing
    audio_allowed = {"mp3", "m4a", "aac", "opus", "flac", "wav"}
    video_allowed = {"mp4", "mkv", "webm", "mov"}

    c = (req.container or "").lower()
    if not c:
        raise HTTPException(400, detail="container is required")

    inferred_mode = None
    if c in audio_allowed:
        inferred_mode = "audio"
    elif c in video_allowed:
        inferred_mode = "video"
    else:
        raise HTTPException(400, detail=f"Unsupported format: {c}")

    mode = req.mode or inferred_mode

    job = new_job(
        url=req.url,
        mode=mode,
        container=c,
        playlist=bool(req.playlist_items or req.selected_urls) or req.playlist,
        playlist_items=req.playlist_items,
        selected_urls=req.selected_urls,
        force_mp4=req.force_mp4,
        cookie_file=req.cookie_file,
    )
    await manager.enqueue(job)
    return DownloadResponse(job_id=job.job_id)


@app.get("/api/progress/{job_id}")
async def sse_progress(job_id: str):
    q = manager.subscribe(job_id)

    async def event_gen():
        # Initial heartbeat
        yield f"retry: {settings.SSE_HEARTBEAT_SEC * 1000}\n\n"
        while True:
            msg = await q.get()
            if msg == "__CLOSE__":
                break
            yield f"data: {msg}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/api/history")
async def get_history(limit: int = 200):
    rows = db.history(limit=limit)
    return JSONResponse(rows)


class OpenFolderRequest(BaseModel):
    path: str


@app.post("/api/cancel/{job_id}")
async def cancel_job(job_id: str):
    """Cancel a running download."""
    success = manager.cancel_job(job_id)
    if success:
        return {"ok": True, "message": "Job marked for cancellation"}
    return {"ok": False, "message": "Job not found or already completed"}


@app.post("/api/open-folder")
async def open_folder(req: OpenFolderRequest):
    p = Path(req.path)
    if not p.exists():
        raise HTTPException(400, detail=f"Path does not exist: {p}")
    system = platform.system()
    try:
        if system == "Windows":
            # Open the real Downloads shell folder when applicable to avoid selection behavior
            import subprocess, os
            try:
                from pathlib import Path as _P
                downloads_path = str(_P.home() / "Downloads")
                if os.path.normcase(str(p)) == os.path.normcase(downloads_path):
                    subprocess.Popen(["explorer", "shell:Downloads"])
                else:
                    subprocess.Popen(["explorer", str(p)])
            except Exception:
                subprocess.Popen(["explorer", str(p)])
        elif system == "Darwin":
            import subprocess
            subprocess.Popen(["open", str(p)])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(p)])
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, detail=str(e))

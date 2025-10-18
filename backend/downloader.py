from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from shutil import which

from fastapi import HTTPException
import yt_dlp as ytdlp

from .settings import settings
from . import db


class YDLLogger:
    def debug(self, msg):
        logger.debug(str(msg))
    def warning(self, msg):
        logger.warning(str(msg))
    def error(self, msg):
        logger.error(str(msg))


@dataclass
class Job:
    job_id: str
    url: str
    mode: str  # 'audio' | 'video'
    container: str  # audio/video container identifier
    playlist: bool
    playlist_items: Optional[list[int]]
    selected_urls: Optional[list[str]]
    cookie_file: Optional[str]


logger = logging.getLogger("rippit.downloader")


def _is_windows_file_lock_error(err: Exception) -> bool:
    """Return True if the exception indicates that Windows denied access to a file in use."""
    cursor: Optional[BaseException] = err  # type: ignore[assignment]
    while cursor is not None:
        try:
            if getattr(cursor, "winerror", None) == 32:  # type: ignore[attr-defined]
                return True
        except Exception:
            pass
        msg = str(cursor).lower()
        if "winerror 32" in msg or "being used by another process" in msg:
            return True
        cursor = getattr(cursor, "__cause__", None) or getattr(cursor, "__context__", None)
    return False


class DownloadManager:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.progress_channels: Dict[str, asyncio.Queue[str]] = {}
        self.running = False
        self.workers: list[asyncio.Task] = []
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.cancelled_jobs: set[str] = set()  # Track cancelled job IDs
        self.active_jobs: Dict[str, bool] = {}  # Track currently running jobs

    def subscribe(self, job_id: str) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue()
        self.progress_channels[job_id] = q
        return q

    async def publish(self, job_id: str, payload: Dict[str, Any]) -> None:
        q = self.progress_channels.get(job_id)
        if q is not None:
            await q.put(json.dumps(payload))

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        # Capture the main event loop for thread-safe callbacks
        self.loop = asyncio.get_running_loop()
        for _ in range(settings.MAX_CONCURRENT):
            self.workers.append(asyncio.create_task(self._worker()))

    async def enqueue(self, job: Job) -> None:
        await self.queue.put(job)
        logger.info("Enqueued job %s url=%s mode=%s container=%s", job.job_id, job.url, job.mode, job.container)
        # Persist initial state
        now = datetime.utcnow().isoformat()
        db.insert_job({
            "job_id": job.job_id,
            "url": job.url,
            "mode": job.mode,
            "container": job.container,
            "status": "queued",
            "started_at": now,
        })
        await self.publish(job.job_id, {"status": "queued", "job_id": job.job_id, "download_dir": str(settings.DOWNLOAD_DIR)})

    def cancel_job(self, job_id: str) -> bool:
        """Mark a job as cancelled. Returns True if the job was active."""
        if job_id in self.active_jobs:
            self.cancelled_jobs.add(job_id)
            logger.info("Job %s marked for cancellation", job_id)
            return True
        return False

    async def _worker(self) -> None:
        while True:
            job = await self.queue.get()
            start_ts = time.time()
            self.active_jobs[job.job_id] = True
            try:
                # Check if cancelled before starting
                if job.job_id in self.cancelled_jobs:
                    logger.info("Job %s was cancelled before starting", job.job_id)
                    await self.publish(job.job_id, {"status": "cancelled"})
                    db.update_job(job.job_id, {
                        "status": "cancelled",
                        "finished_at": datetime.utcnow().isoformat(),
                    })
                else:
                    logger.info("Worker picked job %s", job.job_id)
                    await self._process(job)
                    duration = time.time() - start_ts
                    db.update_job(job.job_id, {"duration_sec": duration})
            except Exception as e:
                # Check if it was cancelled
                if job.job_id in self.cancelled_jobs:
                    logger.info("Job %s cancelled", job.job_id)
                    await self.publish(job.job_id, {"status": "cancelled"})
                    db.update_job(job.job_id, {
                        "status": "cancelled",
                        "finished_at": datetime.utcnow().isoformat(),
                    })
                else:
                    logger.exception("Job %s failed: %s", job.job_id, e)
                    await self.publish(job.job_id, {"status": "error", "error": str(e)})
                    db.update_job(job.job_id, {
                        "status": "error",
                        "error": str(e),
                        "finished_at": datetime.utcnow().isoformat(),
                    })
            finally:
                # Cleanup
                self.active_jobs.pop(job.job_id, None)
                self.cancelled_jobs.discard(job.job_id)
                # Close channel
                ch = self.progress_channels.get(job.job_id)
                if ch:
                    await ch.put("__CLOSE__")
                self.queue.task_done()

    def _build_output_template(self) -> Dict[str, Any]:
        # Flat filename: exactly the video title with extension (no subfolders, no IDs)
        outtmpl = "%(title)s.%(ext)s"
        return {
            "outtmpl": {
                "default": outtmpl,
            }
        }

    def _common_opts(self, job: Job, dest_root: Path) -> Dict[str, Any]:
        dest_root.mkdir(parents=True, exist_ok=True)
        data_dir = settings.DATA_DIR
        data_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = settings.TEMP_DIR
        temp_dir.mkdir(parents=True, exist_ok=True)

        # Try to resolve ffmpeg location explicitly (helps in portable installs)
        ffmpeg_location: Optional[str] = None
        try:
            ff = os.environ.get("FFMPEG_BINARY") or which("ffmpeg")
            if not ff:
                # Try bundled bin directory
                bin_dir = (settings.BASE_DIR / "bin").resolve()
                cand = list(bin_dir.rglob("ffmpeg.exe")) + list(bin_dir.rglob("ffmpeg"))
                if cand:
                    ff = str(cand[0])
            if ff:
                ffmpeg_location = str(Path(ff).parent)
        except Exception:
            ffmpeg_location = None

        is_video = (job.mode or "").lower() == "video"

        opts: Dict[str, Any] = {
            "paths": {
                "home": str(dest_root),
                "temp": str(temp_dir),
            },
            "noplaylist": not job.playlist,
            "restrictfilenames": False,  # allow full titles; Windows invalid chars still sanitized
            "windowsfilenames": True,
            "clean_infojson": True,
            "ignoreconfig": True,  # ignore any system/user yt-dlp config files for consistency
            # Allow re-downloads; do not skip by archive and do not block on existing files
            # We'll let yt-dlp overwrite; future improvement can add auto-rename
            # by computing a unique target name in advance.
            # Do not write separate thumbnail files; still embed where possible
            # (yt-dlp will download thumb temporarily for embedding and clean up)
            "embedthumbnail": True,
            "addmetadata": True,  # FFmpegMetadata
            # Do not use download archive so repeated downloads are allowed
            "overwrites": True,
            "force_overwrites": True,
            "retries": 10,
            "fragment_retries": 10,
            "continuedl": True,
            "concurrent_fragment_downloads": 32 if is_video else 12,
            "http_chunk_size": 67108864 if is_video else 16777216,
            "progress_hooks": [],  # filled later
            "quiet": True,
            "no_warnings": True,
            "verbose": True,
            "logger": YDLLogger(),
            "http_headers": {"User-Agent": "Mozilla/5.0"},
        }
        if ffmpeg_location:
            opts["ffmpeg_location"] = ffmpeg_location
        aria2c = which("aria2c")
        if aria2c:
            parallel = "32" if is_video else "16"
            segments = "32" if is_video else "16"
            chunk = "4M" if is_video else "2M"
            opts["external_downloader"] = "aria2c"
            opts["external_downloader_args"] = {
                "default": [
                    "-c",
                    "--file-allocation=none",
                    "--auto-file-renaming=false",
                    "--optimize-concurrent-downloads=1",
                    f"-x{parallel}",
                    f"-s{segments}",
                    f"-k{chunk}",
                    "--min-split-size=1M",
                    "--summary-interval=0",
                ]
            }
        # Ensure playlist mode flags when user asked for playlist
        if job.playlist or job.playlist_items:
            opts["noplaylist"] = False
            opts["yesplaylist"] = True
        # Limit playlist items if specified (note: may be overridden by URL list mode below)
        if job.playlist_items:
            # yt-dlp accepts comma-separated list or ranges (1-based)
            items_expr = ",".join(str(int(i)) for i in sorted(set(job.playlist_items)))
            opts["playlist_items"] = items_expr
        # Cookie support (explicit file takes precedence)
        if job.cookie_file:
            cookie_path = Path(job.cookie_file).expanduser()
            if cookie_path.exists():
                opts["cookiefile"] = str(cookie_path)
            else:
                raise HTTPException(status_code=400, detail=f"Cookie file not found: {cookie_path}")

        opts.update(self._build_output_template())
        return opts

    def _audio_opts(self, job: Job) -> Dict[str, Any]:
        container = job.container.lower()
        if container == "original":
            return {
                "format": "bestaudio/best",
                "postprocessors": [],
            }

        if container == "opus":
            fmt = "bestaudio[format_id^=251]/bestaudio[acodec^=opus]/bestaudio/best"
            postprocessors = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "opus",
                "preferredquality": "0",
            }]
        elif container == "aac":
            fmt = "bestaudio[acodec^=mp4a][abr>=250]/bestaudio[acodec^=mp4a]/bestaudio/best"
            postprocessors = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "0",
            }]
        elif container == "mp3":
            fmt = "bestaudio/best"
            postprocessors = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }]
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported audio format: {container}")

        return {
            "format": fmt,
            "postprocessors": [
                *postprocessors,
                {"key": "FFmpegMetadata", "add_metadata": True},
            ],
        }

    def _video_opts(self, job: Job) -> Dict[str, Any]:
        c = job.container.lower()
        url_lc = (job.url or "").lower()
        pp = [{"key": "FFmpegMetadata", "add_metadata": True}]
        limit = "[height<=4320][fps<=60]"

        if c == "mp4":
            fmt = (
                f"bestvideo[ext=mp4][vcodec^=avc1]{limit}+bestaudio[ext=m4a]/"
                f"bestvideo[vcodec^=avc1]{limit}+bestaudio[acodec^=mp4a]/"
                f"best[ext=mp4]/best"
            )
            return {
                "format": fmt,
                "merge_output_format": "mp4",
                "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}, *pp],
            }

        if c != "auto":
            raise HTTPException(status_code=400, detail=f"Unsupported video format: {c}")

        if "tiktok.com" in url_lc or "instagram.com" in url_lc:
            fmt = (
                "bestvideo[ext=mp4][height<=1920][fps<=60]+bestaudio[ext=m4a]/"
                "best[ext=mp4]/best"
            )
            return {
                "format": fmt,
                "merge_output_format": "mp4",
                "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}, *pp],
            }

        fmt = (
            f"bestvideo[codec^=av01]{limit}+bestaudio[acodec^=opus]/"
            f"bestvideo[codec^=vp9]{limit}+bestaudio[acodec^=opus]/"
            f"bestvideo[codec^=avc1]{limit}+bestaudio[acodec^=mp4a]/"
            f"bestvideo{limit}+bestaudio/best"
        )
        return {
            "format": fmt,
            "postprocessors": pp,
        }

    async def _process(self, job: Job) -> None:
        await self.publish(job.job_id, {"status": "extracting"})
        db.update_job(job.job_id, {"status": "extracting"})
        logger.info("Start processing job %s (mode=%s, container=%s)", job.job_id, job.mode, job.container)

        dest_root = settings.DOWNLOAD_DIR

        common = self._common_opts(job, dest_root)
        url_lower = (job.url or "").lower()

        # Helper to detect YouTube URLs (often require cookies)
        def _is_youtube(u: str) -> bool:
            lu = (u or "").lower()
            return ("youtube.com" in lu) or ("youtu.be" in lu) or ("music.youtube.com" in lu)

        # Enumerate browser/profile candidates for Chromium-based browsers too
        def _iter_cookie_candidates():
            chromium = {"edge", "chrome", "chromium", "brave", "opera", "vivaldi"}
            profiles = [None, "Default", "Default Profile", *[f"Profile {i}" for i in range(1, 8)]]
            for b in list(getattr(settings, "COOKIES_BROWSERS", [])):
                if b.lower() == "opera":
                    continue
                if b.lower() in chromium:
                    for p in profiles:
                        yield (b, p)
                else:
                    yield (b, None)

        def _select_browser_cookies() -> Optional[tuple[str, Optional[str]]]:
            for (b, p) in _iter_cookie_candidates():
                try:
                    probe_opts = {**common, "skip_download": True}
                    probe_opts["cookiesfrombrowser"] = (b,) if p is None else (b, p)
                    with ytdlp.YoutubeDL(probe_opts) as ydl_probe:
                        ydl_probe.extract_info(job.url, download=False)
                        return (b, p)
                except Exception:
                    continue
            return None

        selected_cookie: Optional[tuple[str, Optional[str]]] = None
        # Prefer cookies up-front for YouTube; for other sites, try no-cookies first
        if settings.AUTO_COOKIES and not job.cookie_file and _is_youtube(job.url):
            selected_cookie = _select_browser_cookies()

        if not selected_cookie:
            # Try without cookies and only fall back if extractor complains
            need_cookies = False
            try:
                with ytdlp.YoutubeDL({**common, "skip_download": True}) as ydl_probe:
                    ydl_probe.extract_info(job.url, download=False)
            except Exception:
                need_cookies = True

            if need_cookies and settings.AUTO_COOKIES and not job.cookie_file:
                selected_cookie = _select_browser_cookies()

        # Progress hook closure
        async def async_publish(payload: Dict[str, Any]):
            await self.publish(job.job_id, payload)

        # Track current item index robustly even if playlist_index isn't provided
        current_index: int = 0
        last_video_id: Optional[str] = None

        logged_formats: set[str] = set()

        def hook(d: Dict[str, Any]):
            nonlocal current_index, last_video_id
            try:
                # Check if job was cancelled
                if job.job_id in self.cancelled_jobs:
                    raise RuntimeError("Download cancelled by user")
                
                st = d.get("status")
                info = d.get("info_dict", {}) or {}
                # Common fields
                pi = info.get("playlist_index")
                vid = info.get("id")
                if not pi and vid:
                    # If we forced per-URL (no playlist), recover original index from pre-extract
                    try:
                        pi = id_to_index.get(vid)
                    except Exception:
                        pi = None
                # Fallback when yt-dlp doesn't provide playlist_index
                if not pi:
                    if vid and vid != last_video_id:
                        current_index += 1
                        last_video_id = vid
                    if current_index <= 0:
                        current_index = 1
                    pi = current_index
                else:
                    try:
                        current_index = int(pi)
                    except Exception:
                        pass
                base = {
                    "status": st,
                    "filename": d.get("filename") or d.get("tmpfilename"),
                    "downloaded_bytes": d.get("downloaded_bytes"),
                    "total_bytes": d.get("total_bytes") or d.get("total_bytes_estimate"),
                    "speed": d.get("speed"),
                    "eta": d.get("eta"),
                    "job_id": job.job_id,
                    "title": info.get("title"),
                    "video_id": vid,
                    "playlist_index": pi,
                    "playlist_title": info.get("playlist_title"),
                    "extractor_key": info.get("extractor_key"),
                    "format_id": info.get("format_id"),
                    "width": info.get("width"),
                    "height": info.get("height"),
                    "fps": info.get("fps"),
                }
                fmt_id = info.get("format_id")
                if st == "downloading" and fmt_id and fmt_id not in logged_formats:
                    logged_formats.add(fmt_id)
                    logger.info(
                        "Job %s using format %s (%sx%s@%sfps) vcodec=%s acodec=%s",
                        job.job_id,
                        fmt_id,
                        info.get("width"),
                        info.get("height"),
                        info.get("fps"),
                        info.get("vcodec"),
                        info.get("acodec"),
                    )
                if st == "downloading":
                    t = base.get("total_bytes") or 0
                    dl = base.get("downloaded_bytes") or 0
                    pct = float(dl) / float(t) * 100.0 if t else None
                    payload = {**base, "progress": {"percent": pct}, "total_items": total_items}
                    if self.loop:
                        asyncio.run_coroutine_threadsafe(async_publish(payload), self.loop)
                    # Update DB lightweight
                    db.update_job(job.job_id, {
                        "status": "downloading",
                        "title": base.get("title"),
                        "video_id": base.get("video_id"),
                        "playlist_index": base.get("playlist_index"),
                        "playlist_title": base.get("playlist_title"),
                        "site": base.get("extractor_key"),
                    })
                elif st == "finished":
                    payload = {**base, "status": "postprocessing", "total_items": total_items}
                    if self.loop:
                        asyncio.run_coroutine_threadsafe(async_publish(payload), self.loop)
                    db.update_job(job.job_id, {"status": "postprocessing"})
            except Exception as e:
                if self.loop:
                    asyncio.run_coroutine_threadsafe(async_publish({"status": "error", "error": str(e)}), self.loop)

        # Build yt-dlp options
        opts = {**common}
        if selected_cookie:
            b, p = selected_cookie
            opts["cookiesfrombrowser"] = (b,) if p is None else (b, p)
        if job.mode == "audio":
            opts.update(self._audio_opts(job))
        else:
            opts.update(self._video_opts(job))
        opts["progress_hooks"] = [hook]

        # Postprocessor hook to capture final filepath and perform container remux if required
        def post_hook(d: Dict[str, Any]):
            try:
                if d.get("status") == "finished":
                    info = d.get("info_dict", {}) or {}
                    fp = info.get("filepath") or info.get("_filename")
                    final_fp = fp
                    if fp:
                        vcodec = (info.get("vcodec") or "").lower()
                        desired_ext: Optional[str] = None
                        if "tiktok.com" in url_lower or "instagram.com" in url_lower:
                            desired_ext = "mp4"
                        elif any(token in vcodec for token in ("avc", "h264")):
                            desired_ext = "mp4"
                        elif any(token in vcodec for token in ("av01", "vp9")):
                            desired_ext = "webm"
                        if desired_ext:
                            path_obj = Path(fp)
                            current_ext = path_obj.suffix.lower().lstrip(".")
                            if desired_ext != current_ext:
                                target = path_obj.with_suffix(f".{desired_ext}")
                                cmd = ["ffmpeg", "-y", "-i", fp, "-c", "copy", str(target)]
                                try:
                                    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                                    if path_obj.exists():
                                        path_obj.unlink()
                                    final_fp = str(target)
                                except Exception as conv_err:
                                    logger.warning("Remux to %s failed for job %s: %s", desired_ext, job.job_id, conv_err)
                    if final_fp and self.loop:
                        asyncio.run_coroutine_threadsafe(
                            async_publish({"status": "postprocessed", "filepath": final_fp}), self.loop
                        )
                    if final_fp:
                        db.update_job(job.job_id, {"filepath": final_fp})
                    elif fp:
                        db.update_job(job.job_id, {"filepath": fp})
            except Exception as ex:
                logger.warning("post_hook failed for job %s: %s", job.job_id, ex)

        opts["postprocessor_hooks"] = [post_hook]

        # Emit initial details for debugging playlist handling
        try:
            await self.publish(job.job_id, {
                "status": "extracting",
                "job_id": job.job_id,
                "playlist": not opts.get("noplaylist", False),
                "playlist_items": opts.get("playlist_items"),
            })
        except Exception:
            pass

        # Pre-extract to count playlist items and resolve selected entries if needed
        total_items = None
        playlist_entries = []
        # Skip pre-extract if we already have explicit URLs from client
        if job.selected_urls:
            selected_urls = list(dict.fromkeys([u for u in job.selected_urls if isinstance(u, str) and u.strip()]))
            total_items = len(selected_urls)
        else:
            try:
                # Use flat extraction for faster playlist metadata
                flat_opts = {**opts, "skip_download": True, "extract_flat": "in_playlist"}
                with ytdlp.YoutubeDL(flat_opts) as ydl:
                    info = ydl.extract_info(job.url, download=False)
                    if info and info.get("_type") == "playlist":
                        playlist_entries = info.get("entries") or []
                        total_items = len(playlist_entries)
                    else:
                        logger.info("Extracted info for job %s: title=%s", job.job_id, (info or {}).get("title"))
            except Exception as ex:
                logger.warning("Pre-extract failed for job %s: %s", job.job_id, ex)

        selected_urls: Optional[list[str]] = None
        id_to_index: dict[str, int] = {}
        
        # If we have explicit URLs from client, skip extraction entirely
        if job.selected_urls:
            selected_urls = list(dict.fromkeys([u for u in job.selected_urls if isinstance(u, str) and u.strip()]))
            total_items = len(selected_urls)
            # Immediately update status
            db.update_job(job.job_id, {"total_items": total_items})
            await self.publish(job.job_id, {"status": "downloading", "total_items": total_items})
        elif playlist_entries:
            # Build mapping from playlist_index -> entry
            idx_map: dict[int, Any] = {}
            for e in playlist_entries:
                pi = e.get("playlist_index")
                if isinstance(pi, int):
                    idx_map[pi] = e
            if job.playlist_items:
                # Resolve only requested indices
                selected_entries = [idx_map[i] for i in sorted(set(job.playlist_items)) if i in idx_map]
            else:
                selected_entries = playlist_entries
            # Resolve URLs and id->index map
            urls: list[str] = []
            for e in selected_entries:
                vid = e.get("id")
                if isinstance(vid, str):
                    id_to_index[vid] = e.get("playlist_index") or 0
                u = e.get("webpage_url") or e.get("url")
                if isinstance(u, str):
                    urls.append(u)
            if urls:
                selected_urls = urls
                total_items = len(urls)
            if total_items:
                db.update_job(job.job_id, {"total_items": total_items})
                await self.publish(job.job_id, {"status": "extracting", "total_items": total_items})
        else:
            # Single item
            if total_items:
                db.update_job(job.job_id, {"total_items": total_items})
                await self.publish(job.job_id, {"status": "extracting", "total_items": total_items})

        # Actual download (blocking -> run in thread)
        def _run_download() -> Optional[str]:
            final_filepath: Optional[str] = None
            # If we resolved a subset of URLs, force no-playlist and download those URLs directly
            real_opts = dict(opts)
            if selected_urls is not None:
                real_opts["noplaylist"] = True
                real_opts.pop("playlist_items", None)
            with ytdlp.YoutubeDL(real_opts) as ydl:
                targets = selected_urls if selected_urls is not None else [job.url]
                # Check cancellation before and during download
                if job.job_id in self.cancelled_jobs:
                    raise RuntimeError("Download cancelled by user")
                code = ydl.download(targets)
                if code not in (0, None):
                    raise RuntimeError(f"yt-dlp exited with code {code}")
                return final_filepath

        try:
            await asyncio.get_running_loop().run_in_executor(None, _run_download)
        except Exception as ex:
            err = ex
            err_s = str(err).lower()

            def _augment_outtmpl_for_lock(base_opts: Dict[str, Any]) -> Dict[str, Any]:
                new_opts = dict(base_opts)
                # Disable overwrite flags so yt-dlp can pick a fresh basename.
                new_opts["overwrites"] = False
                new_opts["force_overwrites"] = False
                outtmpl_cfg = dict(new_opts.get("outtmpl") or {})
                default_tpl = outtmpl_cfg.get("default") or "%(title)s.%(ext)s"
                if "%(epoch)" not in default_tpl:
                    if ".%(ext)s" in default_tpl:
                        default_tpl = default_tpl.replace(".%(ext)s", " [%(epoch)d].%(ext)s")
                    else:
                        default_tpl = f"{default_tpl} [%(epoch)d]"
                outtmpl_cfg["default"] = default_tpl
                new_opts["outtmpl"] = outtmpl_cfg
                return new_opts

            if "aria2c" in err_s and "exited with code" in err_s:
                logger.warning("aria2c failed for job %s (%s); retrying without external downloader", job.job_id, err)
                opts.pop("external_downloader", None)
                opts.pop("external_downloader_args", None)
                try:
                    await self.publish(job.job_id, {"status": "extracting", "note": "Retrying without aria2c"})
                except Exception:
                    pass
                try:
                    await asyncio.get_running_loop().run_in_executor(None, _run_download)
                    err = None
                except Exception as ex2:
                    err = ex2
                    err_s = str(err).lower()
            if err is not None and _is_windows_file_lock_error(err):
                logger.warning("Detected locked output file for job %s; retrying with unique filename", job.job_id)
                opts = _augment_outtmpl_for_lock(opts)
                try:
                    await self.publish(job.job_id, {"status": "extracting", "note": "Target in use, writing new copy"})
                except Exception:
                    pass
                try:
                    await asyncio.get_running_loop().run_in_executor(None, _run_download)
                    err = None
                except Exception as ex2:
                    err = ex2
                    err_s = str(err).lower()
            if err is not None:
                should_retry = (
                    settings.AUTO_COOKIES
                    and not job.cookie_file
                    and not selected_cookie
                    and _is_youtube(job.url)
                    and (
                        "cookies" in err_s
                        or "confirm you" in err_s
                        or "not a bot" in err_s
                        or "403" in err_s
                        or "forbidden" in err_s
                    )
                )
                if should_retry:
                    picked = None
                    for (b, p) in _iter_cookie_candidates():
                        try:
                            probe_opts = {**opts, "skip_download": True}
                            probe_opts["cookiesfrombrowser"] = (b,) if p is None else (b, p)
                            with ytdlp.YoutubeDL(probe_opts) as ydl_probe:
                                ydl_probe.extract_info(job.url, download=False)
                                picked = (b, p)
                                break
                        except Exception:
                            continue
                    if picked:
                        selected_cookie = picked
                        b, p = picked
                        opts["cookiesfrombrowser"] = (b,) if p is None else (b, p)
                        try:
                            await self.publish(job.job_id, {"status": "extracting", "note": "Retrying with browser cookies"})
                        except Exception:
                            pass

                        def _run_download_retry() -> Optional[str]:
                            real_opts = dict(opts)
                            if selected_urls is not None:
                                real_opts["noplaylist"] = True
                                real_opts.pop("playlist_items", None)
                            with ytdlp.YoutubeDL(real_opts) as ydl:
                                targets = selected_urls if selected_urls is not None else [job.url]
                                if job.job_id in self.cancelled_jobs:
                                    raise RuntimeError("Download cancelled by user")
                                code = ydl.download(targets)
                                if code not in (0, None):
                                    raise RuntimeError(f"yt-dlp exited with code {code}")
                                return None

                        await asyncio.get_running_loop().run_in_executor(None, _run_download_retry)
                    else:
                        raise err
                else:
                    raise err

        # Mark done
        now = datetime.utcnow().isoformat()
        db.update_job(job.job_id, {"status": "done", "finished_at": now})
        logger.info("Job %s finished", job.job_id)
        await self.publish(job.job_id, {"status": "done"})


manager = DownloadManager()


def new_job(url: str, mode: str, container: str, playlist: bool, playlist_items: Optional[list[int]], selected_urls: Optional[list[str]], cookie_file: Optional[str]) -> Job:
    job = Job(
        job_id=str(uuid.uuid4()),
        url=url.strip(),
        mode=mode,
        container=container,
        playlist=playlist,
        playlist_items=playlist_items,
        selected_urls=selected_urls,
        cookie_file=cookie_file,
    )
    return job

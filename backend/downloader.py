from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
import logging
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
    container: str  # audio: mp3/m4a/opus/flac/wav; video: mp4/mkv/webm/mov
    playlist: bool
    playlist_items: Optional[list[int]]
    selected_urls: Optional[list[str]]
    force_mp4: bool
    cookie_file: Optional[str]


logger = logging.getLogger("rippit.downloader")


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
            "concurrent_fragment_downloads": 16,
            "http_chunk_size": 10485760,  # 10 MiB chunks may improve throughput
            "progress_hooks": [],  # filled later
            "quiet": True,
            "no_warnings": True,
            "verbose": True,
            "logger": YDLLogger(),
            "http_headers": {"User-Agent": "Mozilla/5.0"},
        }
        if ffmpeg_location:
            opts["ffmpeg_location"] = ffmpeg_location
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
        fmt = "bestaudio/best"
        postprocessors = []
        if container == "mp3":
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            })
        elif container in ("m4a", "aac"):
            # Prefer AAC sources; remux/extract to m4a ~256k without an extra remux step
            fmt = "bestaudio[ext=m4a]/bestaudio[acodec^=aac]/bestaudio/best"
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "256",
            })
        elif container == "opus":
            fmt = "bestaudio[acodec^=opus]/bestaudio/best"
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "opus",
                "preferredquality": "160",
            })
        elif container == "flac":
            postprocessors.append({"key": "FFmpegExtractAudio", "preferredcodec": "flac"})
        elif container == "wav":
            postprocessors.append({"key": "FFmpegExtractAudio", "preferredcodec": "wav"})
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
        pp = [{"key": "FFmpegMetadata", "add_metadata": True}]
        
        # Base format selection - get best quality
        fmt = "bestvideo+bestaudio/best"
        
        if c == "mp4":
            # Add remuxer to ensure MP4 output
            pp.append({"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"})
            opts = {"format": fmt, "merge_output_format": "mp4", "postprocessors": pp}
            return opts
        elif c == "mov":
            # MOV needs H.264+AAC: merge to MP4 first (fast), then remux to MOV
            fmt = (
                "bv*[vcodec^=avc1]+ba[acodec^=aac]/"  # h264+aac if available
                "bv*[ext=mp4]+ba[ext=m4a]/"            # mp4+m4a pairs
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                "bestvideo+bestaudio/best"
            )
            # First remux to MP4 to ensure compatible codecs, then convert MP4->MOV
            pp.append({"key": "FFmpegVideoConvertor", "preferedformat": "mov"})
            return {
                "format": fmt,
                "merge_output_format": "mp4",  # Merge to MP4 first (safe)
                "postprocessors": pp,
                # Force H.264/AAC encoding if codecs aren't compatible
                "postprocessor_args": {
                    "ffmpeg": [
                        "-c:v", "libx264",
                        "-pix_fmt", "yuv420p",
                        "-preset", "medium",
                        "-crf", "20",
                        "-c:a", "aac",
                        "-b:a", "160k",
                        "-movflags", "+faststart",
                    ]
                },
            }
        elif c == "mkv":
            # Add remuxer to ensure MKV output
            pp.append({"key": "FFmpegVideoRemuxer", "preferedformat": "mkv"})
            return {"format": fmt, "merge_output_format": "mkv", "postprocessors": pp}
        elif c == "webm":
            # Try to get WebM source when possible, but force WebM output
            fmt = (
                "bv*[ext=webm]+ba[ext=webm]/"
                "bestvideo[ext=webm]+bestaudio[ext=webm]/"
                "bestvideo+bestaudio/best"
            )
            pp.append({"key": "FFmpegVideoRemuxer", "preferedformat": "webm"})
            return {"format": fmt, "merge_output_format": "webm", "postprocessors": pp}
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported video format: {c}")

    async def _process(self, job: Job) -> None:
        await self.publish(job.job_id, {"status": "extracting"})
        db.update_job(job.job_id, {"status": "extracting"})
        logger.info("Start processing job %s (mode=%s, container=%s)", job.job_id, job.mode, job.container)

        dest_root = settings.DOWNLOAD_DIR

        common = self._common_opts(job, dest_root)

        # Helper to detect YouTube URLs (often require cookies)
        def _is_youtube(u: str) -> bool:
            lu = (u or "").lower()
            return ("youtube.com" in lu) or ("youtu.be" in lu) or ("music.youtube.com" in lu)

        # Enumerate browser/profile candidates for Chromium-based browsers too
        def _iter_cookie_candidates():
            chromium = {"edge", "chrome", "chromium", "brave", "opera", "vivaldi"}
            profiles = [None, "Default", "Default Profile", *[f"Profile {i}" for i in range(1, 8)]]
            for b in list(getattr(settings, "COOKIES_BROWSERS", [])):
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

        def hook(d: Dict[str, Any]):
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
                }
                if st == "downloading":
                    t = base.get("total_bytes") or 0
                    dl = base.get("downloaded_bytes") or 0
                    pct = float(dl) / float(t) * 100.0 if t else None
                    payload = {**base, "progress": {"percent": pct}}
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
                    payload = {**base, "status": "postprocessing"}
                    if self.loop:
                        asyncio.run_coroutine_threadsafe(async_publish(payload), self.loop)
                    db.update_job(job.job_id, {"status": "postprocessing"})
            except Exception as e:
                if self.loop:
                    asyncio.run_coroutine_threadsafe(async_publish({"status": "error", "error": str(e)}), self.loop)

        # Build yt-dlp options
        opts = {**common}
        # For YouTube, prefer the Android player client which often avoids extra verification prompts
        if _is_youtube(job.url):
            ea = dict(opts.get("extractor_args") or {})
            yargs = dict((ea.get("youtube") or {}))
            yargs["player_client"] = ["android"]
            ea["youtube"] = yargs
            opts["extractor_args"] = ea
        if selected_cookie:
            b, p = selected_cookie
            opts["cookiesfrombrowser"] = (b,) if p is None else (b, p)
        if job.mode == "audio":
            opts.update(self._audio_opts(job))
        else:
            opts.update(self._video_opts(job))
        opts["progress_hooks"] = [hook]

        # Postprocessor hook to capture final filepath and status
        def post_hook(d: Dict[str, Any]):
            try:
                if d.get("status") == "finished":
                    info = d.get("info_dict", {}) or {}
                    fp = info.get("filepath") or info.get("_filename")
                    if fp:
                        if self.loop:
                            asyncio.run_coroutine_threadsafe(
                                async_publish({"status": "postprocessed", "filepath": fp}), self.loop
                            )
                        db.update_job(job.job_id, {"filepath": fp})
            except Exception:
                pass

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
            # If YouTube threw a bot/cookies error and we didn't use cookies yet, retry once with browser cookies
            err_s = str(ex).lower()
            should_retry = (
                settings.AUTO_COOKIES
                and not job.cookie_file
                and not selected_cookie
                and _is_youtube(job.url)
                and ("cookies" in err_s or "confirm you" in err_s or "not a bot" in err_s)
            )
            if should_retry:
                # Attempt to pick a browser (and profile) now
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
                    # Re-run download with cookies
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
                    raise
            else:
                raise

        # Mark done
        now = datetime.utcnow().isoformat()
        db.update_job(job.job_id, {"status": "done", "finished_at": now})
        logger.info("Job %s finished", job.job_id)
        await self.publish(job.job_id, {"status": "done"})


manager = DownloadManager()


def new_job(url: str, mode: str, container: str, playlist: bool, playlist_items: Optional[list[int]], selected_urls: Optional[list[str]], force_mp4: bool, cookie_file: Optional[str]) -> Job:
    job = Job(
        job_id=str(uuid.uuid4()),
        url=url.strip(),
        mode=mode,
        container=container,
        playlist=playlist,
        playlist_items=playlist_items,
        selected_urls=selected_urls,
        force_mp4=force_mp4,
        cookie_file=cookie_file,
    )
    return job

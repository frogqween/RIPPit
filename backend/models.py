from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


Mode = Literal["audio", "video"]


class DownloadRequest(BaseModel):
    url: str
    # mode is now optional; backend will infer from container if not provided
    mode: Optional[Mode] = None
    container: str = Field(description="Audio: mp3|m4a|opus|flac|wav; Video: mp4|mkv|webm")
    playlist: bool = False
    playlist_items: Optional[list[int]] = None  # list of 1-based indices to download
    selected_urls: Optional[list[str]] = None  # explicit list of entry URLs to download (overrides playlist_items)
    force_mp4: bool = False
    cookie_file: Optional[str] = None


class DownloadResponse(BaseModel):
    job_id: str


class HistoryItem(BaseModel):
    job_id: str
    url: str
    mode: Mode
    container: str
    site: Optional[str] = None
    title: Optional[str] = None
    video_id: Optional[str] = None
    status: str
    filepath: Optional[str] = None
    playlist_title: Optional[str] = None
    playlist_index: Optional[int] = None
    total_items: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_sec: Optional[float] = None
    error: Optional[str] = None

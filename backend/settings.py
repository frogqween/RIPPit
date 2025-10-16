from __future__ import annotations

from pathlib import Path
import platform


class Settings:
    APP_NAME: str = "RIPPit"

    # Concurrency
    MAX_CONCURRENT: int = 2

    # Paths
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    DATA_DIR: Path = BASE_DIR / "data"
    TEMP_DIR: Path = DATA_DIR / "temp"

    # Default download dirs per OS
    SYSTEM = platform.system()
    if SYSTEM == "Windows":
        # Use the main system Downloads folder
        DOWNLOAD_DIR: Path = Path.home() / "Downloads"
    elif SYSTEM == "Darwin":
        # Use the main system Downloads folder
        DOWNLOAD_DIR: Path = Path.home() / "Downloads"
    else:
        # Reasonable default for Linux/others
        DOWNLOAD_DIR: Path = Path.home() / "Downloads"

    ARCHIVE_FILE: Path = DATA_DIR / "archive.txt"  # yt-dlp download archive (skip dupes)
    DB_FILE: Path = DATA_DIR / "history.db"

    # SSE / progress
    SSE_HEARTBEAT_SEC: int = 15

    # Cookies: automatically use browser cookies where possible
    AUTO_COOKIES: bool = True
    if SYSTEM == "Windows":
        COOKIES_BROWSERS = ["edge", "chrome", "firefox", "brave", "opera"]
    elif SYSTEM == "Darwin":
        COOKIES_BROWSERS = ["safari", "chrome", "firefox", "brave"]
    else:
        COOKIES_BROWSERS = ["chrome", "firefox"]

    # Frontend
    STATIC_DIR: Path = BASE_DIR / "frontend" / "static"


settings = Settings()

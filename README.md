# RIPPit

Local downloader UI powered by FastAPI + yt-dlp + FFmpeg.

What it does (v1)
- Paste a URL from YouTube, SoundCloud, TikTok, Instagram, X/Twitter, NTS, etc.
- Choose Audio (mp3, m4a/aac, opus, flac, wav) or Video (mp4, mkv, webm)
- Optional: Download entire playlist (checkbox)
- Optional: Cookie file for your own account (auth-only content where supported)
- Two concurrent downloads, queued beyond that
- Live progress bars (per-item) and simple history
- Output structure: {site}/{uploader_or_channel}/{playlist_title?}/{playlist_index} - {title} [id].ext
  - Site comes from yt-dlp extractor key (e.g., YouTube, Soundcloud)
  - Missing fields are skipped (no empty folders)

Defaults per your spec
- Windows default download dir: %USERPROFILE%/Desktop/Downloads
- macOS default download dir: ~/Downloads/RIPPit
- MP3: 320 kbps CBR
- AAC/M4A & Opus: prefer source codec; remux when possible; otherwise re-encode (~256k AAC, ~160k Opus)
- Video MP4: prefer H.264; if not available, falls back to WebM/MKV without re-encode unless you toggle "Force MP4 (re-encode)"

Prereqs
- Python 3.11+ recommended
- FFmpeg available on PATH (yt-dlp uses it for muxing/transcoding)
  - Windows: https://www.gyan.dev/ffmpeg/builds/ (add bin to PATH) or install via winget
  - macOS: brew install ffmpeg

Setup (Windows PowerShell)
1) Create venv and install deps
   - py -m venv .venv
   - .\.venv\Scripts\python -m pip install -U pip
   - .\.venv\Scripts\python -m pip install -r requirements.txt

2) Run the server
   - .\.venv\Scripts\python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000

3) Open UI
   - http://localhost:8000

Notes
- Duplicate skipping: uses yt-dlp download archive at data/archive.txt
- Metadata/cover art: enabled where possible via ffmpeg
- If you select MP4 but the best stream is WebM/AV1 and you don’t force re-encode, it will fall back to WebM or MKV to avoid quality loss.
- If a site changes, updating yt-dlp usually fixes it: .\.venv\Scripts\python -m pip install -U yt-dlp

Planned (v1.1)
- Playlist range selection
- Advanced containers (mov), ogg option, richer per-site quality picks
- Desktop wrapper (Tauri)

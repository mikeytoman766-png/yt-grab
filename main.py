"""YT Grab - FastAPI backend. Only download content you have permission to save."""
import asyncio
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
DL_DIR = BASE_DIR / "downloads"
DL_DIR.mkdir(exist_ok=True)

COOKIES = os.getenv("YTDLP_COOKIES")
_cookie_text = os.getenv("YTDLP_COOKIES_CONTENT")
if _cookie_text:
    COOKIES = str(BASE_DIR / "cookies.txt")
    Path(COOKIES).write_text(_cookie_text)

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                 "music.youtube.com", "youtu.be"}
TIERS = {2160: "4K (2160p)", 1440: "2K (1440p)", 1080: "1080p", 720: "720p", 480: "480p"}
FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

slots = asyncio.Semaphore(2)

for stale in DL_DIR.iterdir():
    stale.unlink(missing_ok=True)

app = FastAPI(title="YT Grab")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length"],
)


def check_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise HTTPException(400, "That doesn't look like a YouTube link.")
    return url.strip()


def friendly(err: Exception) -> HTTPException:
    msg = str(err).lower()
    print("yt-dlp error:", err, flush=True)  # shows in Render logs
    if "not a bot" in msg:
        return HTTPException(429, "YouTube is asking this server to prove it's not a bot. Add a cookies file (YTDLP_COOKIES_CONTENT).")
    if "confirm your age" in msg or "age-restricted" in msg:
        return HTTPException(403, "This video is age-restricted. The server needs a logged-in cookies file to fetch it.")
    if "private video" in msg:
        return HTTPException(403, "This video is private.")
    if "not available in your country" in msg or "blocked" in msg:
        return HTTPException(403, "This video is blocked in the server's region.")
    if "unavailable" in msg or "removed" in msg or "does not exist" in msg:
        return HTTPException(404, "This video is unavailable or has been removed.")
    if "unsupported url" in msg or "valid url" in msg:
        return HTTPException(400, "That doesn't look like a valid video link.")
    return HTTPException(502, "Couldn't fetch this video: " + str(err)[:300])


def base_opts() -> dict:
    opts = {
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": ["default", "tv", "web_safari"]}},
    }
    if COOKIES:
        opts["cookiefile"] = COOKIES
    return opts


def safe_name(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", title).strip(". ")[:120] or "video"


def cleanup(job: str) -> None:
    for p in DL_DIR.glob(f"{job}*"):
        p.unlink(missing_ok=True)


def build_options(info: dict) -> list[dict]:
    best: dict[int, tuple] = {}
    for f in info.get("formats", []):
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        tier = next((t for t in TIERS if abs(h - t) <= t * 0.1), None)
        if tier is None:
            continue
        score = (str(f.get("vcodec", "")).startswith("avc1"), f.get("fps") or 0, f.get("tbr") or 0)
        if tier not in best or score > best[tier][0]:
            best[tier] = (score, f)

    options = []
    for tier in sorted(best, reverse=True):
        f = best[tier][1]
        size = f.get("filesize") or f.get("filesize_approx")
        options.append({
            "format_id": f["format_id"],
            "label": TIERS[tier],
            "kind": "video",
            "size_mb": round(size / 1_048_576) if size else None,
        })
    options.append({"format_id": "mp3", "label": "Audio only (MP3)", "kind": "audio", "size_mb": None})
    return options


def run_download(url: str, format_id: str, job: str) -> tuple[Path, str, str]:
    opts = base_opts() | {"outtmpl": str(DL_DIR / f"{job}.%(ext)s")}
    if format_id == "mp3":
        ext = "mp3"
        opts |= {
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3", "preferredquality": "320"}],
        }
    else:
        ext = "mp4"
        opts |= {
            "format": f"{format_id}+bestaudio[ext=m4a]/{format_id}+bestaudio/best",
            "merge_output_format": "mp4",
            "postprocessor_args": {"merger": ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]},
        }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    path = DL_DIR / f"{job}.{ext}"
    if not path.exists():
        raise RuntimeError("Output file missing after merge")
    return path, info.get("title", "video"), ext


@app.get("/inspect")
async def inspect(url: str = Query(..., min_length=5)):
    url = check_url(url)
    try:
        with yt_dlp.YoutubeDL(base_opts()) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, False)
    except Exception as e:
        raise friendly(e)
    return {
        "title": info.get("title", "Untitled"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "options": build_options(info),
    }


class DownloadRequest(BaseModel):
    url: str
    format_id: str


@app.post("/download")
async def download(req: DownloadRequest, background_tasks: BackgroundTasks):
    url = check_url(req.url)
    if not FORMAT_ID_RE.match(req.format_id):
        raise HTTPException(400, "Invalid format.")
    job = uuid.uuid4().hex
    async with slots:
        try:
            path, title, ext = await asyncio.to_thread(run_download, url, req.format_id, job)
        except yt_dlp.utils.DownloadError as e:
            cleanup(job)
            raise friendly(e)
        except Exception as e:
            cleanup(job)
            print("download error:", repr(e), flush=True)
            raise HTTPException(500, "Processing failed: " + str(e)[:300])

    background_tasks.add_task(cleanup, job)
    return FileResponse(
        path,
        media_type="audio/mpeg" if ext == "mp3" else "video/mp4",
        filename=f"{safe_name(title)}.{ext}",
        background=background_tasks,
    )


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")

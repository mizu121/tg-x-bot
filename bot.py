import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
import yt_dlp
from apify_client import ApifyClient
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger(__name__)

DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "49"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
SEND_AS_DOCUMENT_MB = int(os.getenv("SEND_AS_DOCUMENT_MB", "45"))
SEND_AS_DOCUMENT_BYTES = SEND_AS_DOCUMENT_MB * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1"))
MAX_REELS_PER_REQUEST = int(os.getenv("MAX_REELS_PER_REQUEST", "5"))
YTDLP_FORMAT = os.getenv(
    "YTDLP_FORMAT",
    "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/best[height<=720]/best",
)
YTDLP_COOKIE_FILE = os.getenv("YTDLP_COOKIE_FILE")
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
APIFY_INSTAGRAM_ACTOR = os.getenv("APIFY_INSTAGRAM_ACTOR", "apify/instagram-scraper")

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
USERNAME_RE = re.compile(r"^@(?P<username>[A-Za-z0-9_.]+)(?:\s+(?P<count>\d+))?$")
REELS_RE = re.compile(r"^!reels\s+(?P<username>[A-Za-z0-9_.]+)(?:\s+(?P<count>\d+))?$", re.IGNORECASE)

download_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


class DownloadTooLargeError(Exception):
    pass


class MissingConfigError(Exception):
    pass


@dataclass
class DownloadResult:
    file_path: Path
    caption: str


def _build_ydl_opts(job_dir: Path) -> dict:
    opts = {
        "format": YTDLP_FORMAT,
        "paths": {"home": str(job_dir)},
        "outtmpl": {"default": "%(extractor_key)s-%(id)s.%(ext)s"},
        "merge_output_format": "mp4",
        "prefer_ffmpeg": True,
        "keepvideo": False,
        "noplaylist": True,
        "cachedir": False,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 30,
        "extractor_args": {
            "instagram": {
                "direct": True,
            }
        },
    }

    if YTDLP_COOKIE_FILE:
        opts["cookiefile"] = YTDLP_COOKIE_FILE

    return opts


def _trim_caption(text: str, limit: int = 3900) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 20].rstrip()}\n\n...[truncated]"


def _caption_from_info(info: dict) -> str:
    title = info.get("title") or "Video"
    uploader = info.get("uploader") or info.get("channel")
    description = info.get("description")

    parts = [f"Video: {title}"]
    if uploader:
        parts.append(f"By: {uploader}")
    if description:
        parts.append(description)

    return _trim_caption("\n\n".join(parts))


def _caption_from_apify_item(item: dict, prefix: str = "Instagram video") -> str:
    parts = [prefix]
    owner = item.get("ownerUsername") or item.get("username")
    if owner:
        parts.append(f"By: @{owner}")
    if item.get("caption"):
        parts.append(str(item["caption"]))
    metrics = []
    if item.get("likesCount") is not None:
        metrics.append(f"{item['likesCount']} likes")
    if item.get("videoViewCount") is not None:
        metrics.append(f"{item['videoViewCount']} views")
    if item.get("commentsCount") is not None:
        metrics.append(f"{item['commentsCount']} comments")
    if metrics:
        parts.append(" | ".join(metrics))
    return _trim_caption("\n\n".join(parts), limit=1024)


def _resolve_downloaded_file(info: dict, ydl: yt_dlp.YoutubeDL, job_dir: Path) -> Path:
    for requested in info.get("requested_downloads") or []:
        filepath = requested.get("filepath")
        if filepath and Path(filepath).exists():
            return Path(filepath)

    prepared = Path(ydl.prepare_filename(info))
    if prepared.exists():
        return prepared

    files = [path for path in job_dir.iterdir() if path.is_file() and not path.name.endswith(".part")]
    if not files:
        raise FileNotFoundError("Download finished but no output file was created.")

    return max(files, key=lambda path: path.stat().st_mtime)


def _check_size(file_path: Path) -> None:
    size = file_path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        size_mb = size / 1024 / 1024
        raise DownloadTooLargeError(
            f"Downloaded file is {size_mb:.1f} MB, above the configured {MAX_UPLOAD_MB} MB limit."
        )


def _download_direct_video(url: str, job_dir: Path, caption: str = "Video") -> DownloadResult:
    file_path = job_dir / "direct-video.mp4"
    downloaded = 0

    with requests.get(url, stream=True, timeout=(10, 60)) as response:
        response.raise_for_status()

        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > MAX_UPLOAD_BYTES:
            raise DownloadTooLargeError(f"Remote file is above the configured {MAX_UPLOAD_MB} MB limit.")

        with file_path.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > MAX_UPLOAD_BYTES:
                    raise DownloadTooLargeError(f"Remote file is above the configured {MAX_UPLOAD_MB} MB limit.")
                output.write(chunk)

    return DownloadResult(file_path=file_path, caption=caption)


def download_video(url: str, job_dir: Path, caption: str | None = None) -> DownloadResult:
    if "scontent" in url or "cdninstagram" in url:
        return _download_direct_video(url, job_dir, caption=caption or "Video")

    with yt_dlp.YoutubeDL(_build_ydl_opts(job_dir)) as ydl:
        info = ydl.extract_info(url, download=True)
        if not isinstance(info, dict):
            raise ValueError("Could not read video metadata.")

        file_path = _resolve_downloaded_file(info, ydl, job_dir)
        _check_size(file_path)
        return DownloadResult(file_path=file_path, caption=caption or _caption_from_info(info))


def _apify_client() -> ApifyClient:
    if not APIFY_TOKEN:
        raise MissingConfigError("APIFY_TOKEN is not set.")
    return ApifyClient(APIFY_TOKEN)


def fetch_instagram_items(direct_urls: Iterable[str], limit: int) -> list[dict]:
    run_input = {
        "directUrls": list(direct_urls),
        "resultsType": "posts",
        "resultsLimit": limit,
        "searchLimit": 1,
    }
    client = _apify_client()
    run = client.actor(APIFY_INSTAGRAM_ACTOR).call(run_input=run_input)
    return list(client.dataset(run["defaultDatasetId"]).iterate_items())


def video_url_from_item(item: dict) -> str | None:
    return item.get("videoUrl") or item.get("videoUrlDownload")


async def send_download(context: ContextTypes.DEFAULT_TYPE, chat_id: int, result: DownloadResult) -> None:
    file_size = result.file_path.stat().st_size
    with result.file_path.open("rb") as video_file:
        if file_size > SEND_AS_DOCUMENT_BYTES:
            await context.bot.send_document(chat_id=chat_id, document=video_file, caption=result.caption[:1024])
        else:
            await context.bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=result.caption[:1024],
                supports_streaming=True,
            )


async def process_and_send_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    caption: str | None = None,
) -> None:
    if not update.message:
        return

    chat_id = update.message.chat_id
    job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=DOWNLOAD_ROOT))
    try:
        async with download_slots:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
            result = await asyncio.to_thread(download_video, url, job_dir, caption)
        await send_download(context, chat_id, result)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def handle_instagram_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    if not update.message:
        return

    if not APIFY_TOKEN:
        await process_and_send_url(update, context, url)
        return

    status_message = await update.message.reply_text("Processing Instagram content...")
    items = await asyncio.to_thread(fetch_instagram_items, [url], 1)
    for item in items:
        video_url = video_url_from_item(item)
        if not video_url:
            await status_message.edit_text("This Instagram item does not look like a video or reel.")
            return
        caption = _caption_from_apify_item(item)
        await process_and_send_url(update, context, video_url, caption)
        await status_message.edit_text("Done.")
        return

    await status_message.edit_text("No downloadable Instagram video was found.")


async def handle_reels(update: Update, context: ContextTypes.DEFAULT_TYPE, username: str, count: int) -> None:
    if not update.message:
        return

    count = max(1, min(count, MAX_REELS_PER_REQUEST))
    status_message = await update.message.reply_text(f"Fetching up to {count} reels from @{username}...")
    items = await asyncio.to_thread(fetch_instagram_items, [f"https://www.instagram.com/{username}/reels/"], count)

    sent = 0
    for item in items:
        video_url = video_url_from_item(item)
        if not video_url:
            continue
        sent += 1
        await status_message.edit_text(f"Downloading reel {sent}/{count} from @{username}...")
        caption = _caption_from_apify_item(item, prefix=f"Reel {sent}/{count}")
        await process_and_send_url(update, context, video_url, caption)
        if sent >= count:
            break

    if sent:
        await status_message.edit_text(f"Finished sending {sent} reel(s) from @{username}.")
    else:
        await status_message.edit_text(f"No downloadable reels were found for @{username}.")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    reels_match = REELS_RE.match(text)
    username_match = USERNAME_RE.match(text)
    if reels_match or username_match:
        match = reels_match or username_match
        username = match.group("username")
        count = int(match.group("count") or MAX_REELS_PER_REQUEST)
        try:
            await handle_reels(update, context, username, count)
        except MissingConfigError:
            await update.message.reply_text("Instagram reel scraping needs APIFY_TOKEN configured on the server.")
        except Exception:
            logger.exception("Failed fetching reels for %s", username)
            await update.message.reply_text("Sorry, I could not fetch those reels.")
        return

    match = URL_RE.search(text)
    if not match:
        await update.message.reply_text("Send me a video link and I will try to download it.")
        return

    url = match.group(0).rstrip(".,)")
    await update.message.reply_text("Processing your request...")

    try:
        if "instagram.com" in url:
            await handle_instagram_url(update, context, url)
        else:
            await process_and_send_url(update, context, url)
    except DownloadTooLargeError as exc:
        await update.message.reply_text(f"File is too large for this bot config: {exc}")
    except MissingConfigError as exc:
        await update.message.reply_text(str(exc))
    except yt_dlp.utils.DownloadError as exc:
        logger.warning("yt-dlp failed for %s: %s", url, exc)
        await update.message.reply_text("Sorry, I could not download that link. It may be private or blocked.")
    except Exception:
        logger.exception("Unexpected error while handling %s", url)
        await update.message.reply_text("Sorry, something went wrong while downloading that video.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Hi. Send me a video link from YouTube, X/Twitter, Instagram, or TikTok. "
        "You can also send @username 3 to fetch recent Instagram reels when Apify is configured."
    )


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set. Add it as an environment variable before starting the bot.")

    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

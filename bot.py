import asyncio
import base64
import html
import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
import yt_dlp
from apify_client import ApifyClient
from dotenv import load_dotenv
from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _prepare_cookie_file() -> str | None:
    cookie_file = os.getenv("YTDLP_COOKIE_FILE")
    cookie_blob = os.getenv("YTDLP_COOKIES_B64")
    if not cookie_blob:
        return cookie_file

    target = Path(os.getenv("YTDLP_GENERATED_COOKIE_FILE", "/tmp/ytdlp_cookies.txt"))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(cookie_blob))
        target.chmod(0o600)
        return str(target)
    except Exception as exc:
        logger.warning("Could not prepare yt-dlp cookie file: %s", exc)
        return cookie_file


def _csv_values(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _youtube_client_groups(value: str | None) -> list[list[str]]:
    groups = []
    for group in (value or "").split(";"):
        clients = _csv_values(group)
        if clients:
            groups.append(clients)
    return groups

STARTED_AT = time.time()
DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
FAILURE_LOG_PATH = Path(os.getenv("FAILURE_LOG_PATH", "logs/failures.jsonl"))
FAILURE_LOG_MAX_ENTRIES = int(os.getenv("FAILURE_LOG_MAX_ENTRIES", "100"))
MESSAGE_LOG_PATH = Path(os.getenv("MESSAGE_LOG_PATH", "logs/messages.jsonl"))
MESSAGE_LOG_MAX_ENTRIES = int(os.getenv("MESSAGE_LOG_MAX_ENTRIES", "500"))
LOG_FULL_URLS = os.getenv("LOG_FULL_URLS", "false").lower() == "true"
ADMIN_CHAT_IDS = {
    value.strip() for value in os.getenv("ADMIN_CHAT_IDS", "").split(",") if value.strip()
}
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "49"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
SEND_AS_DOCUMENT_MB = int(os.getenv("SEND_AS_DOCUMENT_MB", "45"))
SEND_AS_DOCUMENT_BYTES = SEND_AS_DOCUMENT_MB * 1024 * 1024
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1"))
MAX_REELS_PER_REQUEST = int(os.getenv("MAX_REELS_PER_REQUEST", "5"))
DOWNLOAD_TTL_MINUTES = int(os.getenv("DOWNLOAD_TTL_MINUTES", "90"))
DOWNLOAD_MAX_TOTAL_MB = int(os.getenv("DOWNLOAD_MAX_TOTAL_MB", "600"))
PROGRESS_INTERVAL_SECONDS = float(os.getenv("PROGRESS_INTERVAL_SECONDS", "2.5"))
PROGRESS_FRAMES = tuple(os.getenv("PROGRESS_FRAMES", "|/-\\"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "90"))
YTDLP_FORMAT = os.getenv(
    "YTDLP_FORMAT",
    "bv*[height<=720]+ba/b[height<=720]/best[height<=720]/best",
)
YTDLP_COOKIE_FILE = _prepare_cookie_file()
YOUTUBE_CLIENTS = _csv_values(os.getenv("YOUTUBE_CLIENTS", "mweb,web_safari,android,web"))
YOUTUBE_FALLBACK_CLIENTS = _youtube_client_groups(
    os.getenv("YOUTUBE_FALLBACK_CLIENTS", "android,web;web_safari;tv;ios")
)
YOUTUBE_PO_PROVIDER = os.getenv("YOUTUBE_PO_PROVIDER", "none").strip().lower()
YOUTUBE_BGUTIL_BASE_URL = os.getenv("YOUTUBE_BGUTIL_BASE_URL", "").strip()
YOUTUBE_BGUTIL_SERVER_HOME = os.getenv("YOUTUBE_BGUTIL_SERVER_HOME", "").strip()
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
APIFY_INSTAGRAM_ACTOR = os.getenv("APIFY_INSTAGRAM_ACTOR", "apify/instagram-scraper")
APIFY_MAX_CHARGE_USD = os.getenv("APIFY_MAX_CHARGE_USD")
TELEGRAM_MESSAGE_EFFECT_ID = os.getenv("TELEGRAM_MESSAGE_EFFECT_ID", "").strip()
LOADER_STICKER_FILE_ID = os.getenv("LOADER_STICKER_FILE_ID", "").strip()
LOADER_ANIMATION_FILE_ID = os.getenv("LOADER_ANIMATION_FILE_ID", "").strip()

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
USERNAME_RE = re.compile(r"^@(?P<username>[A-Za-z0-9_.]+)(?:\s+(?P<count>\d+))?$")
REELS_RE = re.compile(r"^!reels\s+(?P<username>[A-Za-z0-9_.]+)(?:\s+(?P<count>\d+))?$", re.IGNORECASE)

VIDEO_URL_KEYS = ("videoUrl", "videoUrlDownload", "video_url", "video", "downloadUrl")
PHOTO_URL_KEYS = (
    "displayUrl",
    "display_url",
    "displaySrc",
    "imageUrl",
    "image_url",
    "image",
    "photoUrl",
    "thumbnailUrl",
    "thumbnail_src",
)
CHILD_POST_KEYS = (
    "childPosts",
    "children",
    "carouselMedia",
    "sidecarChildren",
    "sidecar_to_children",
    "media",
)
SAFE_QUERY_KEYS = {"v", "list", "t", "start", "end"}
MAX_MEDIA_GROUP_ITEMS = 10

download_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


class DownloadTooLargeError(Exception):
    pass


class MissingConfigError(Exception):
    pass


@dataclass
class MediaItem:
    url: str
    kind: str
    caption: str
    label: str = "media"


@dataclass
class DownloadResult:
    file_path: Path
    caption: str
    kind: str
    title: str = "Media"
    uploader: str | None = None
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    source: str | None = None


class ProgressMessage:
    def __init__(self, message, initial_step: str, cleanup_messages: list | None = None) -> None:
        self.message = message
        self.step = initial_step
        self.history = [initial_step]
        self.started_at = time.time()
        self.done = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.last_text: tuple[str | None, str] | None = None
        self.cleanup_messages = cleanup_messages or []

    async def start(self) -> None:
        await self._edit(self._render(), parse_mode="HTML")
        self.task = asyncio.create_task(self._run())

    async def set(self, step: str) -> None:
        self.step = step
        if not self.history or self.history[-1] != step:
            self.history.append(step)
            self.history = self.history[-5:]
        await self._edit(self._render(), parse_mode="HTML")

    async def finish(self, final_text: str | None = None) -> None:
        self.done.set()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if final_text:
            await self._delete_cleanup_messages()
            await self._edit(final_text)

    async def delete(self) -> None:
        self.done.set()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        await self._delete_cleanup_messages()
        try:
            await self.message.delete()
        except TelegramError as exc:
            logger.debug("Progress delete failed: %s", exc)

    async def _delete_cleanup_messages(self) -> None:
        for message in self.cleanup_messages:
            try:
                await message.delete()
            except TelegramError as exc:
                logger.debug("Cleanup message delete failed: %s", exc)
        self.cleanup_messages = []

    async def _run(self) -> None:
        while not self.done.is_set():
            await asyncio.sleep(PROGRESS_INTERVAL_SECONDS)
            await self._edit(self._render(), parse_mode="HTML")

    async def _edit(self, text: str, parse_mode: str | None = None) -> None:
        current = (parse_mode, text)
        if current == self.last_text:
            return
        self.last_text = current
        try:
            await self.message.edit_text(text, parse_mode=parse_mode, disable_web_page_preview=True)
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                logger.debug("Progress edit failed: %s", exc)
        except TelegramError as exc:
            logger.debug("Progress edit failed: %s", exc)

    def _render(self) -> str:
        elapsed = max(1, int(time.time() - self.started_at))
        frame = PROGRESS_FRAMES[elapsed % len(PROGRESS_FRAMES)] if PROGRESS_FRAMES else "*"
        width = 12
        cursor = elapsed % width
        bar = ["▱"] * width
        for offset in range(4):
            bar[(cursor - offset) % width] = "▰"
        minutes, seconds = divmod(elapsed, 60)
        stage_line = "  ".join(self._stage_tokens())
        trace = " → ".join(self._clip(step, 18) for step in self.history[-3:])
        lines = [
            f"<b>{html.escape(self._clip(self.step, 42))}</b>",
            f"<code>{''.join(bar)}</code>  {minutes:02d}:{seconds:02d}  {html.escape(frame)}",
            html.escape(stage_line),
            f"<i>{html.escape(trace)}</i>",
        ]
        return "\n".join(lines)

    def _stage_tokens(self) -> list[str]:
        stages = [
            ("READ", ("reading request", "booting")),
            ("FETCH", ("reading instagram", "fetching", "analyzing", "found")),
            ("DL", ("downloading",)),
            ("UPLOAD", ("uploading",)),
            ("DONE", ("done", "sent")),
        ]
        current = self.step.lower()
        history = " ".join(self.history).lower()
        tokens = []
        for label, needles in stages:
            if any(needle in current for needle in needles):
                state = "●"
            elif any(needle in history for needle in needles):
                state = "✓"
            else:
                state = "·"
            tokens.append(f"{label} {state}")
        return tokens

    @staticmethod
    def _clip(value: str, limit: int) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) <= limit:
            return cleaned
        return f"{cleaned[: limit - 3].rstrip()}..."


def _message_effect_kwargs(update: Update) -> dict:
    if TELEGRAM_MESSAGE_EFFECT_ID and update.effective_chat and update.effective_chat.type == "private":
        return {"message_effect_id": TELEGRAM_MESSAGE_EFFECT_ID}
    return {}


def _message_effect_kwargs_for_chat(chat_type: str | None) -> dict:
    if TELEGRAM_MESSAGE_EFFECT_ID and chat_type == "private":
        return {"message_effect_id": TELEGRAM_MESSAGE_EFFECT_ID}
    return {}


async def tracked_reply(update: Update, text: str, **kwargs):
    if not update.message:
        return None
    message = await update.message.reply_text(text, **kwargs)
    record_bot_messages(update.message.chat_id, message)
    return message


async def send_loader(context: ContextTypes.DEFAULT_TYPE, chat_id: int, chat_type: str | None):
    effect_kwargs = _message_effect_kwargs_for_chat(chat_type)
    try:
        if LOADER_STICKER_FILE_ID:
            message = await context.bot.send_sticker(
                chat_id=chat_id,
                sticker=LOADER_STICKER_FILE_ID,
                **effect_kwargs,
            )
            record_bot_messages(chat_id, message)
            return message
        if LOADER_ANIMATION_FILE_ID:
            message = await context.bot.send_animation(
                chat_id=chat_id,
                animation=LOADER_ANIMATION_FILE_ID,
                **effect_kwargs,
            )
            record_bot_messages(chat_id, message)
            return message
    except TelegramError as exc:
        logger.info("Loader animation failed: %s", exc)
    return None


def _hostname(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _chat_id(update: Update) -> str | None:
    if update.effective_chat:
        return str(update.effective_chat.id)
    if update.message:
        return str(update.message.chat_id)
    return None


def _user_id(update: Update) -> str | None:
    if update.effective_user:
        return str(update.effective_user.id)
    return None


def _is_admin(update: Update) -> bool:
    if not ADMIN_CHAT_IDS:
        return False
    ids = {_chat_id(update), _user_id(update)}
    return bool(ADMIN_CHAT_IDS.intersection(value for value in ids if value))


def _sanitize_url(url: str) -> str:
    if LOG_FULL_URLS:
        return url
    parsed = urlparse(url)
    safe_query = [(key, value) for key, value in parse_qsl(parsed.query) if key in SAFE_QUERY_KEYS]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(safe_query), ""))


def _request_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(".,)")


def _platform_from_text(text: str) -> str:
    url = _request_url(text)
    if url:
        return _hostname(url)
    if text.strip().startswith("@") or text.strip().lower().startswith("!reels"):
        return "instagram-reels"
    return "unknown"


def _trim_error(value: str, limit: int = 900) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 15].rstrip()}...[truncated]"


class YtdlpLogBridge:
    def debug(self, msg: str) -> None:
        return

    def warning(self, msg: str) -> None:
        logger.debug("yt-dlp warning: %s", _trim_error(msg))

    def error(self, msg: str) -> None:
        logger.debug("yt-dlp error: %s", _trim_error(msg))


def _failure_record(update: Update, request_text: str, exc: BaseException, stage: str) -> dict:
    url = _request_url(request_text)
    record = {
        "ts": _now_iso(),
        "stage": stage,
        "platform": _platform_from_text(request_text),
        "chat_id": _chat_id(update),
        "user_id": _user_id(update),
        "request": _sanitize_url(url) if url else request_text.strip()[:160],
        "error_type": type(exc).__name__,
        "error": _trim_error(str(exc) or repr(exc)),
    }
    if os.getenv("LOG_TRACEBACKS", "false").lower() == "true":
        record["traceback"] = _trim_error("".join(traceback.format_exception(exc)), limit=3000)
    return record


def record_failure(update: Update, request_text: str, exc: BaseException, stage: str) -> None:
    record = _failure_record(update, request_text, exc, stage)
    logger.warning("media_request_failed %s", json.dumps(record, sort_keys=True))

    try:
        FAILURE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
        trim_failure_log()
    except OSError as log_error:
        logger.warning("Could not write failure log: %s", log_error)


def trim_failure_log() -> None:
    if FAILURE_LOG_MAX_ENTRIES <= 0 or not FAILURE_LOG_PATH.exists():
        return
    lines = FAILURE_LOG_PATH.read_text(encoding="utf-8").splitlines()
    if len(lines) <= FAILURE_LOG_MAX_ENTRIES:
        return
    FAILURE_LOG_PATH.write_text("\n".join(lines[-FAILURE_LOG_MAX_ENTRIES:]) + "\n", encoding="utf-8")


def read_failures(limit: int = 5) -> list[dict]:
    if not FAILURE_LOG_PATH.exists():
        return []
    lines = FAILURE_LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    failures = []
    for line in lines:
        try:
            failures.append(json.loads(line))
        except json.JSONDecodeError:
            failures.append({"ts": "unknown", "error": line[:240]})
    return failures


def clear_failures() -> int:
    if not FAILURE_LOG_PATH.exists():
        return 0
    count = len(FAILURE_LOG_PATH.read_text(encoding="utf-8").splitlines())
    FAILURE_LOG_PATH.write_text("", encoding="utf-8")
    return count


def trim_message_log() -> None:
    if MESSAGE_LOG_MAX_ENTRIES <= 0 or not MESSAGE_LOG_PATH.exists():
        return
    lines = MESSAGE_LOG_PATH.read_text(encoding="utf-8").splitlines()
    if len(lines) <= MESSAGE_LOG_MAX_ENTRIES:
        return
    MESSAGE_LOG_PATH.write_text("\n".join(lines[-MESSAGE_LOG_MAX_ENTRIES:]) + "\n", encoding="utf-8")


def record_bot_messages(chat_id: int | str, messages) -> None:
    if messages is None:
        return
    if not isinstance(messages, (list, tuple)):
        messages = [messages]

    records = []
    for message in messages:
        message_id = getattr(message, "message_id", None)
        if message_id is None:
            continue
        records.append({"ts": _now_iso(), "chat_id": str(chat_id), "message_id": int(message_id)})
    if not records:
        return

    try:
        MESSAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with MESSAGE_LOG_PATH.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
        trim_message_log()
    except OSError as exc:
        logger.debug("Could not write message log: %s", exc)


def read_tracked_message_ids(chat_id: int | str, limit: int) -> list[int]:
    if not MESSAGE_LOG_PATH.exists():
        return []
    seen = set()
    message_ids = []
    for line in reversed(MESSAGE_LOG_PATH.read_text(encoding="utf-8").splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(record.get("chat_id")) != str(chat_id):
            continue
        message_id = record.get("message_id")
        if not isinstance(message_id, int) or message_id in seen:
            continue
        seen.add(message_id)
        message_ids.append(message_id)
        if len(message_ids) >= limit:
            break
    return message_ids


def _format_bytes(size: int) -> str:
    if size >= 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024 / 1024:.1f} GB"
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _format_duration(seconds: int | None) -> str | None:
    if not seconds:
        return None
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _trim_caption(text: str, limit: int = 3900) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 20].rstrip()}\n\n...[truncated]"


def _youtube_extractor_args(clients: list[str] | None = None) -> dict:
    args = {
        "instagram": {"direct": True},
        "youtube": {"player_client": clients or YOUTUBE_CLIENTS},
    }

    if YOUTUBE_PO_PROVIDER == "http":
        args["youtubepot-bgutilhttp"] = {}
        if YOUTUBE_BGUTIL_BASE_URL:
            args["youtubepot-bgutilhttp"]["base_url"] = YOUTUBE_BGUTIL_BASE_URL
    elif YOUTUBE_PO_PROVIDER == "script":
        args["youtubepot-bgutilscript"] = {}
        if YOUTUBE_BGUTIL_SERVER_HOME:
            args["youtubepot-bgutilscript"]["server_home"] = YOUTUBE_BGUTIL_SERVER_HOME

    return args


def _build_ydl_opts(job_dir: Path, youtube_clients: list[str] | None = None) -> dict:
    opts = {
        "format": YTDLP_FORMAT,
        "format_sort": ["res:720", "ext:mp4:m4a"],
        "paths": {"home": str(job_dir)},
        "outtmpl": {"default": "%(extractor_key)s-%(id)s.%(ext)s"},
        "merge_output_format": "mp4",
        "prefer_ffmpeg": True,
        "keepvideo": False,
        "noplaylist": True,
        "cachedir": False,
        "quiet": True,
        "no_warnings": True,
        "logger": YtdlpLogBridge(),
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            )
        },
        "extractor_args": _youtube_extractor_args(youtube_clients),
    }

    if YTDLP_COOKIE_FILE:
        opts["cookiefile"] = YTDLP_COOKIE_FILE

    return opts


def _is_youtube_url(url: str) -> bool:
    host = _hostname(url)
    return host in {"youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"} or host.endswith(".youtube.com")


def _youtube_error_allows_fallback(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        needle in message
        for needle in (
            "sign in to confirm",
            "not a bot",
            "http error 403",
            "forbidden",
            "po token",
            "video unavailable",
            "requested format is not available",
        )
    )


def _youtube_client_attempts() -> list[list[str]]:
    attempts = []
    for clients in [YOUTUBE_CLIENTS, *YOUTUBE_FALLBACK_CLIENTS]:
        if clients and clients not in attempts:
            attempts.append(clients)
    return attempts


def _caption_from_info(info: dict, file_path: Path) -> str:
    title = info.get("title") or "Video"
    uploader = info.get("uploader") or info.get("channel")
    description = info.get("description")
    details = []
    duration = _format_duration(info.get("duration"))
    if uploader:
        details.append(f"By: {uploader}")
    if duration:
        details.append(f"Duration: {duration}")
    if file_path.exists():
        details.append(f"Size: {_format_bytes(file_path.stat().st_size)}")
    if info.get("width") and info.get("height"):
        details.append(f"Quality: {info['width']}x{info['height']}")

    parts = [f"Video: {title}"]
    if details:
        parts.append(" | ".join(details))
    if description:
        parts.append(description)

    return _trim_caption("\n\n".join(parts))


def _caption_from_apify_item(item: dict, prefix: str = "Instagram media") -> str:
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


def _result_caption(result: DownloadResult) -> str:
    details = []
    if result.uploader:
        details.append(f"By: {result.uploader}")
    duration = _format_duration(result.duration)
    if duration:
        details.append(f"Duration: {duration}")
    if result.file_path.exists():
        details.append(f"Size: {_format_bytes(result.file_path.stat().st_size)}")
    if result.width and result.height:
        details.append(f"Quality: {result.width}x{result.height}")
    if result.source:
        details.append(f"Source: {result.source}")

    caption = result.caption or result.title
    if details:
        caption = f"{caption}\n\n" + " | ".join(details)
    return _trim_caption(caption, limit=1024)


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


def _extension_for_url(url: str, content_type: str | None, kind: str) -> str:
    path_ext = Path(urlparse(url).path).suffix
    if path_ext and len(path_ext) <= 6:
        return path_ext
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return guessed
    return ".jpg" if kind == "photo" else ".mp4"


def _download_direct_media(url: str, job_dir: Path, caption: str, kind: str) -> DownloadResult:
    job_dir.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=(10, REQUEST_TIMEOUT_SECONDS)) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type")
        file_path = job_dir / f"direct-media{_extension_for_url(url, content_type, kind)}"
        downloaded = 0

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

    return DownloadResult(file_path=file_path, caption=caption, kind=kind, title=caption, source=_hostname(url))


def _download_with_ydlp(
    url: str,
    job_dir: Path,
    caption: str | None = None,
    youtube_clients: list[str] | None = None,
) -> DownloadResult:
    job_dir.mkdir(parents=True, exist_ok=True)
    with yt_dlp.YoutubeDL(_build_ydl_opts(job_dir, youtube_clients)) as ydl:
        info = ydl.extract_info(url, download=True)
        if not isinstance(info, dict):
            raise ValueError("Could not read video metadata.")

        file_path = _resolve_downloaded_file(info, ydl, job_dir)
        _check_size(file_path)
        title = info.get("title") or "Video"
        return DownloadResult(
            file_path=file_path,
            caption=caption or _caption_from_info(info, file_path),
            kind="video",
            title=title,
            uploader=info.get("uploader") or info.get("channel"),
            duration=info.get("duration"),
            width=info.get("width"),
            height=info.get("height"),
            source=info.get("extractor_key") or _hostname(url),
        )


def download_video(url: str, job_dir: Path, caption: str | None = None) -> DownloadResult:
    if "scontent" in url or "cdninstagram" in url:
        return _download_direct_media(url, job_dir, caption or "Video", "video")

    if not _is_youtube_url(url):
        return _download_with_ydlp(url, job_dir, caption)

    last_error: yt_dlp.utils.DownloadError | None = None
    for index, clients in enumerate(_youtube_client_attempts(), start=1):
        attempt_dir = job_dir / f"youtube-attempt-{index}"
        try:
            if index > 1:
                logger.info("Retrying YouTube download with clients=%s", ",".join(clients))
            return _download_with_ydlp(url, attempt_dir, caption, clients)
        except yt_dlp.utils.DownloadError as exc:
            last_error = exc
            if not _youtube_error_allows_fallback(exc):
                raise

    if last_error:
        raise last_error
    return _download_with_ydlp(url, job_dir, caption)


def download_media_item(item: MediaItem, job_dir: Path) -> DownloadResult:
    if item.kind == "photo":
        return _download_direct_media(item.url, job_dir, item.caption, "photo")
    return download_video(item.url, job_dir, item.caption)


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
    call_kwargs = {"run_input": run_input, "logger": None}
    if APIFY_MAX_CHARGE_USD:
        call_kwargs["max_total_charge_usd"] = Decimal(APIFY_MAX_CHARGE_USD)

    run = client.actor(APIFY_INSTAGRAM_ACTOR).call(**call_kwargs)
    if run is None:
        raise RuntimeError("Apify actor did not return a run.")

    dataset_id = getattr(run, "default_dataset_id", None)
    if dataset_id is None and isinstance(run, dict):
        dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise RuntimeError("Apify actor finished without a dataset.")

    return list(client.dataset(dataset_id).iterate_items())


def _first_url(data: dict, keys: Iterable[str]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
        if isinstance(value, dict):
            nested = _first_url(value, ("url", "src", "uri"))
            if nested:
                return nested
    return None


def _nested_children(data: dict) -> list[dict]:
    children = []
    for key in CHILD_POST_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            children.extend(entry for entry in value if isinstance(entry, dict))
        elif isinstance(value, dict):
            edges = value.get("edges")
            if isinstance(edges, list):
                for edge in edges:
                    node = edge.get("node") if isinstance(edge, dict) else None
                    if isinstance(node, dict):
                        children.append(node)
            nested_media = value.get("media")
            if isinstance(nested_media, list):
                children.extend(entry for entry in nested_media if isinstance(entry, dict))
    return children


def _image_urls(data: dict) -> list[str]:
    urls = []
    for key in ("images", "displayResources", "display_resources"):
        value = data.get(key)
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, str) and entry.startswith("http"):
                    urls.append(entry)
                elif isinstance(entry, dict):
                    nested = _first_url(entry, ("url", "src", "uri", "displayUrl", "imageUrl"))
                    if nested:
                        urls.append(nested)
    image_versions = data.get("image_versions2")
    if isinstance(image_versions, dict):
        candidates = image_versions.get("candidates")
        if isinstance(candidates, list):
            for entry in candidates:
                if isinstance(entry, dict):
                    nested = _first_url(entry, ("url", "src", "uri"))
                    if nested:
                        urls.append(nested)
    return urls


def _media_from_single_item(data: dict, caption: str, label: str) -> list[MediaItem]:
    items = []
    video_url = _first_url(data, VIDEO_URL_KEYS)
    if not video_url and isinstance(data.get("video_versions"), list):
        for entry in data["video_versions"]:
            if isinstance(entry, dict):
                video_url = _first_url(entry, ("url", "src", "uri"))
                if video_url:
                    break
    if video_url:
        items.append(MediaItem(url=video_url, kind="video", caption=caption, label=label))
        return items

    photo_url = _first_url(data, PHOTO_URL_KEYS)
    if photo_url:
        items.append(MediaItem(url=photo_url, kind="photo", caption=caption, label=label))

    for image_url in _image_urls(data):
        items.append(MediaItem(url=image_url, kind="photo", caption=caption, label=label))

    return items


def media_items_from_apify_item(item: dict, prefix: str = "Instagram media") -> list[MediaItem]:
    children = _nested_children(item)
    base_caption = _caption_from_apify_item(item, prefix)
    raw_items = []
    if children:
        total = len(children)
        for index, child in enumerate(children, start=1):
            caption = f"Slide {index}/{total}\n\n{base_caption}"
            raw_items.extend(_media_from_single_item(child, caption, f"slide {index}/{total}"))
    else:
        raw_items.extend(_media_from_single_item(item, base_caption, "media"))

    seen = set()
    unique_items = []
    for media in raw_items:
        if media.url in seen:
            continue
        seen.add(media.url)
        unique_items.append(media)
    return unique_items


def cleanup_downloads() -> dict:
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    now = time.time()
    ttl_seconds = DOWNLOAD_TTL_MINUTES * 60
    max_total_bytes = DOWNLOAD_MAX_TOTAL_MB * 1024 * 1024
    removed_count = 0
    removed_bytes = 0

    files = [path for path in DOWNLOAD_ROOT.rglob("*") if path.is_file()]
    for path in files:
        try:
            if now - path.stat().st_mtime > ttl_seconds:
                size = path.stat().st_size
                path.unlink()
                removed_count += 1
                removed_bytes += size
        except FileNotFoundError:
            pass

    files = [path for path in DOWNLOAD_ROOT.rglob("*") if path.is_file()]
    total_bytes = sum(path.stat().st_size for path in files)
    if total_bytes > max_total_bytes:
        for path in sorted(files, key=lambda item: item.stat().st_mtime):
            if total_bytes <= max_total_bytes:
                break
            try:
                size = path.stat().st_size
                path.unlink()
                total_bytes -= size
                removed_count += 1
                removed_bytes += size
            except FileNotFoundError:
                pass

    for path in sorted((p for p in DOWNLOAD_ROOT.rglob("*") if p.is_dir()), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass

    return {"removed_count": removed_count, "removed_bytes": removed_bytes, "current_bytes": total_bytes}


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


async def send_download(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    result: DownloadResult,
    message_effect_id: str | None = None,
) -> None:
    caption = _result_caption(result)
    file_size = result.file_path.stat().st_size
    effect_kwargs = {"message_effect_id": message_effect_id} if message_effect_id else {}
    with result.file_path.open("rb") as media_file:
        try:
            if result.kind == "photo" and file_size <= SEND_AS_DOCUMENT_BYTES:
                sent_message = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=media_file,
                    caption=caption[:1024],
                    **effect_kwargs,
                )
                record_bot_messages(chat_id, sent_message)
                return
            if result.kind == "video" and file_size <= SEND_AS_DOCUMENT_BYTES:
                sent_message = await context.bot.send_video(
                    chat_id=chat_id,
                    video=media_file,
                    caption=caption[:1024],
                    supports_streaming=True,
                    **effect_kwargs,
                )
                record_bot_messages(chat_id, sent_message)
                return
        except TelegramError as exc:
            logger.info("Inline media upload failed, retrying as document: %s", exc)

        media_file.seek(0)
        sent_message = await context.bot.send_document(
            chat_id=chat_id,
            document=media_file,
            caption=caption[:1024],
            **effect_kwargs,
        )
        record_bot_messages(chat_id, sent_message)


def _can_send_in_media_group(result: DownloadResult) -> bool:
    return result.kind in {"photo", "video"} and result.file_path.stat().st_size <= SEND_AS_DOCUMENT_BYTES


async def send_media_group(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    results: list[DownloadResult],
    message_effect_id: str | None = None,
) -> bool:
    if len(results) < 2 or not all(_can_send_in_media_group(result) for result in results):
        return False

    sent_any = False
    for chunk_start in range(0, len(results), MAX_MEDIA_GROUP_ITEMS):
        chunk = results[chunk_start : chunk_start + MAX_MEDIA_GROUP_ITEMS]
        files = []
        media = []
        try:
            for index, result in enumerate(chunk):
                media_file = result.file_path.open("rb")
                files.append(media_file)
                caption = _result_caption(result)[:1024] if index == 0 else None
                if result.kind == "photo":
                    media.append(InputMediaPhoto(media=media_file, caption=caption))
                else:
                    media.append(InputMediaVideo(media=media_file, caption=caption, supports_streaming=True))
            effect_kwargs = {"message_effect_id": message_effect_id} if message_effect_id else {}
            sent_messages = await context.bot.send_media_group(chat_id=chat_id, media=media, **effect_kwargs)
            record_bot_messages(chat_id, sent_messages)
            sent_any = True
        except TelegramError as exc:
            logger.info("Media group upload failed: %s", exc)
            if sent_any:
                raise
            return False
        finally:
            for media_file in files:
                media_file.close()

    return True


async def process_and_send_media_items(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    items: list[MediaItem],
    progress: ProgressMessage,
) -> None:
    if not update.message or not items:
        return

    chat_id = update.message.chat_id
    effect_kwargs = _message_effect_kwargs(update)
    message_effect_id = effect_kwargs.get("message_effect_id")
    job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=DOWNLOAD_ROOT))
    results: list[DownloadResult] = []
    try:
        async with download_slots:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
            for index, item in enumerate(items, start=1):
                item_dir = job_dir / f"item-{index}"
                await progress.set(f"Downloading {item.label}")
                results.append(await asyncio.to_thread(download_media_item, item, item_dir))

        if len(results) > 1:
            await progress.set(f"Uploading album {len(results)} item(s)")
            if await send_media_group(context, chat_id, results, message_effect_id=message_effect_id):
                return

        for index, result in enumerate(results, start=1):
            suffix = f" {index}/{len(results)}" if len(results) > 1 else ""
            await progress.set(f"Uploading media{suffix}")
            await send_download(context, chat_id, result, message_effect_id=message_effect_id)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        await asyncio.to_thread(cleanup_downloads)


async def process_and_send_media_item(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    item: MediaItem,
    progress: ProgressMessage,
) -> None:
    await process_and_send_media_items(update, context, [item], progress)


async def process_and_send_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    progress: ProgressMessage,
    caption: str | None = None,
) -> None:
    item = MediaItem(url=url, kind="video", caption=caption or "Video", label="video")
    await process_and_send_media_item(update, context, item, progress)


async def handle_instagram_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, progress: ProgressMessage) -> None:
    if not update.message:
        return

    if not APIFY_TOKEN:
        await progress.set("Apify not configured, trying direct download")
        await process_and_send_url(update, context, url, progress)
        return

    await progress.set("Reading Instagram post")
    items = await asyncio.to_thread(fetch_instagram_items, [url], 1)
    for item in items:
        media_items = media_items_from_apify_item(item)
        if not media_items:
            await progress.finish("No downloadable Instagram media was found.")
            return

        total = len(media_items)
        await progress.set(f"Found Instagram album: {total} item(s)" if total > 1 else "Found Instagram media")
        for index, media in enumerate(media_items, start=1):
            media.label = f"{media.label} {index}/{total}" if total > 1 else media.label
        await process_and_send_media_items(update, context, media_items, progress)
        await progress.delete()
        return

    await progress.finish("No downloadable Instagram media was found.")


async def handle_reels(update: Update, context: ContextTypes.DEFAULT_TYPE, username: str, count: int, progress: ProgressMessage) -> None:
    if not update.message:
        return

    count = max(1, min(count, MAX_REELS_PER_REQUEST))
    await progress.set(f"Fetching up to {count} reels from @{username}")
    items = await asyncio.to_thread(fetch_instagram_items, [f"https://www.instagram.com/{username}/reels/"], count)

    sent = 0
    for item in items:
        media_items = media_items_from_apify_item(item, prefix=f"Reel {sent + 1}/{count}")
        for media in media_items[:1]:
            sent += 1
            media.label = f"reel {sent}/{count}"
            await process_and_send_media_item(update, context, media, progress)
            if sent >= count:
                break
        if sent >= count:
            break

    if sent:
        await progress.delete()
    else:
        await progress.finish(f"No downloadable reels were found for @{username}.")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    cleanup = await asyncio.to_thread(cleanup_downloads)
    if cleanup["removed_count"]:
        logger.info("Cleaned %s stale download files", cleanup["removed_count"])

    text = update.message.text.strip()
    loader_message = await send_loader(
        context,
        update.message.chat_id,
        update.effective_chat.type if update.effective_chat else None,
    )
    status_message = await tracked_reply(
        update,
        "Booting media pipeline...",
        **_message_effect_kwargs(update),
    )
    progress = ProgressMessage(status_message, "Reading request", cleanup_messages=[loader_message] if loader_message else [])
    await progress.start()

    try:
        reels_match = REELS_RE.match(text)
        username_match = USERNAME_RE.match(text)
        if reels_match or username_match:
            match = reels_match or username_match
            username = match.group("username")
            count = int(match.group("count") or MAX_REELS_PER_REQUEST)
            await handle_reels(update, context, username, count, progress)
            return

        match = URL_RE.search(text)
        if not match:
            await progress.finish("Send me a video, Instagram post, or @username 3 for reels.")
            return

        url = match.group(0).rstrip(".,)")
        if "instagram.com" in url:
            await handle_instagram_url(update, context, url, progress)
        else:
            await progress.set("Analyzing link with yt-dlp")
            await process_and_send_url(update, context, url, progress)
            await progress.delete()
    except DownloadTooLargeError as exc:
        record_failure(update, text, exc, "download_size")
        await progress.finish(
            f"File is too large for Telegram Bot API upload. {exc}\n\n"
            "I keep the limit just under 50 MB because Telegram cloud bots reject larger uploads."
        )
    except MissingConfigError as exc:
        record_failure(update, text, exc, "config")
        await progress.finish(str(exc))
    except yt_dlp.utils.DownloadError as exc:
        record_failure(update, text, exc, "yt_dlp")
        await progress.finish("Could not download that link. It may be private, blocked, age-gated, or need cookies.")
    except Exception as exc:
        record_failure(update, text, exc, "unexpected")
        logger.exception("Unexpected error while handling request")
        await progress.finish("Something went wrong while downloading that media.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await tracked_reply(
        update,
        "Send a YouTube, X/Twitter, TikTok, or Instagram link. "
        "For Instagram reels, send @username 3 or !reels username 3. "
        "Use /demo to preview the progress HUD or /clean 30 to remove recent bot clutter.",
        **_message_effect_kwargs(update),
    )


async def demo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    loader_message = await send_loader(
        context,
        update.message.chat_id,
        update.effective_chat.type if update.effective_chat else None,
    )
    status_message = await tracked_reply(
        update,
        "Booting media pipeline...",
        **_message_effect_kwargs(update),
    )
    progress = ProgressMessage(status_message, "Reading request", cleanup_messages=[loader_message] if loader_message else [])
    await progress.start()
    for step in (
        "Reading Instagram post",
        "Found Instagram album: 4 item(s)",
        "Downloading slide 1/4",
        "Downloading slide 2/4",
        "Uploading album 4 item(s)",
    ):
        await asyncio.sleep(1.2)
        await progress.set(step)
    await asyncio.sleep(1.2)
    await progress.finish("Demo complete. Real jobs remove this panel after upload.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    cleanup = await asyncio.to_thread(cleanup_downloads)
    disk = shutil.disk_usage(DOWNLOAD_ROOT)
    uptime = int(time.time() - STARTED_AT)
    minutes, seconds = divmod(uptime, 60)
    hours, minutes = divmod(minutes, 60)
    download_size = directory_size(DOWNLOAD_ROOT)

    lines = [
        "Bot status",
        f"Uptime: {hours}h {minutes}m {seconds}s",
        f"Disk free: {_format_bytes(disk.free)} / {_format_bytes(disk.total)}",
        f"Download cache: {_format_bytes(download_size)}",
        f"Cleanup removed: {cleanup['removed_count']} file(s), {_format_bytes(cleanup['removed_bytes'])}",
        f"Max upload: {MAX_UPLOAD_MB} MB (Telegram cloud cap: 50 MB)",
        f"Max concurrent downloads: {MAX_CONCURRENT_DOWNLOADS}",
        f"Max reels/request: {MAX_REELS_PER_REQUEST}",
        f"Apify: {'configured' if APIFY_TOKEN else 'not configured'}",
        f"yt-dlp cookies: {'configured' if YTDLP_COOKIE_FILE else 'not configured'}",
        f"YouTube clients: {','.join(YOUTUBE_CLIENTS)}",
        f"YouTube PO provider: {YOUTUBE_PO_PROVIDER}",
        f"Failure log: {FAILURE_LOG_PATH}",
        f"Message log: {MESSAGE_LOG_PATH}",
        f"Loader media: {'sticker' if LOADER_STICKER_FILE_ID else 'animation' if LOADER_ANIMATION_FILE_ID else 'not configured'}",
        f"Admin commands: {'configured' if ADMIN_CHAT_IDS else 'not configured'}",
        f"Your chat id: {_chat_id(update)}",
    ]
    await tracked_reply(update, "\n".join(lines), **_message_effect_kwargs(update))


async def failures(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not _is_admin(update):
        await tracked_reply(
            update,
            f"Set ADMIN_CHAT_IDS={_chat_id(update)} in the host env to enable failure-log access.",
            **_message_effect_kwargs(update),
        )
        return

    if context.args and context.args[0].lower() in {"clear", "reset"}:
        removed = clear_failures()
        await tracked_reply(
            update,
            f"Cleared {removed} failure log entr{'y' if removed == 1 else 'ies'}.",
            **_message_effect_kwargs(update),
        )
        return

    limit = 5
    if context.args:
        try:
            limit = max(1, min(20, int(context.args[0])))
        except ValueError:
            limit = 5

    entries = read_failures(limit)
    if not entries:
        await tracked_reply(update, "No failures logged yet.", **_message_effect_kwargs(update))
        return

    lines = [f"Last {len(entries)} failure(s)"]
    for entry in entries:
        lines.append(
            "\n".join(
                [
                    f"{entry.get('ts', 'unknown')} | {entry.get('platform', 'unknown')} | {entry.get('stage', 'unknown')}",
                    f"Request: {entry.get('request', 'unknown')}",
                    f"{entry.get('error_type', 'Error')}: {entry.get('error', '')}",
                ]
            )
        )
    await tracked_reply(update, _trim_caption("\n\n".join(lines), limit=3900), **_message_effect_kwargs(update))


async def clean(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not _is_admin(update):
        await tracked_reply(
            update,
            f"Set ADMIN_CHAT_IDS={_chat_id(update)} in the host env to enable cleanup access.",
            **_message_effect_kwargs(update),
        )
        return

    limit = 30
    if context.args:
        try:
            limit = max(1, min(100, int(context.args[0])))
        except ValueError:
            limit = 30

    chat_id = update.message.chat_id
    message_ids = read_tracked_message_ids(chat_id, limit)
    deleted = 0
    failed = 0
    for message_id in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted += 1
        except TelegramError:
            failed += 1

    try:
        await update.message.delete()
    except TelegramError:
        pass

    sent_message = await context.bot.send_message(
        chat_id=chat_id,
        text=f"Cleaned {deleted} bot message(s). {failed} could not be removed.",
        **_message_effect_kwargs(update),
    )
    record_bot_messages(chat_id, sent_message)


async def fileid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not _is_admin(update):
        await tracked_reply(
            update,
            f"Set ADMIN_CHAT_IDS={_chat_id(update)} in the host env to enable file-id access.",
            **_message_effect_kwargs(update),
        )
        return

    target = update.message.reply_to_message or update.message
    label = None
    file_id = None
    if target.sticker:
        label = "sticker"
        file_id = target.sticker.file_id
    elif target.animation:
        label = "animation"
        file_id = target.animation.file_id
    elif target.document:
        label = "document"
        file_id = target.document.file_id
    elif target.video:
        label = "video"
        file_id = target.video.file_id
    elif target.photo:
        label = "photo"
        file_id = target.photo[-1].file_id

    if not file_id:
        await tracked_reply(update, "Reply to a sticker, GIF, photo, video, or file with /fileid.", **_message_effect_kwargs(update))
        return

    await tracked_reply(update, f"{label} file_id:\n<code>{html.escape(file_id)}</code>", parse_mode="HTML")


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await tracked_reply(
        update,
        f"Chat ID: {_chat_id(update)}\nUser ID: {_user_id(update)}",
        **_message_effect_kwargs(update),
    )


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set. Add it as an environment variable before starting the bot.")

    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    cleanup_downloads()

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("demo", demo))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("failures", failures))
    application.add_handler(CommandHandler("clean", clean))
    application.add_handler(CommandHandler("fileid", fileid))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

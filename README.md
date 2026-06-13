# Telegram X Video Downloader Bot

A Telegram polling bot that downloads videos from YouTube, X/Twitter, Instagram, and TikTok. It can also fetch recent Instagram reels with Apify when `APIFY_TOKEN` is configured.

## Required config

Copy `.env.example` to `.env` locally or set the same variables in your host:

```sh
BOT_TOKEN=your_telegram_bot_token
APIFY_TOKEN=optional_apify_token_for_instagram_reels
APIFY_MAX_CHARGE_USD=0.05
ADMIN_CHAT_IDS=your_telegram_chat_id_for_failure_logs
```

Do not commit `.env`, cookie files, Telegram tokens, Apify tokens, or Instagram session cookies.

## Cost and safety defaults

The bot defaults are intentionally conservative:

- `MAX_CONCURRENT_DOWNLOADS=1` prevents multiple large downloads from filling a small server.
- `MAX_UPLOAD_MB=49` avoids downloading files that Telegram is unlikely to accept.
- `MAX_REELS_PER_REQUEST=5` keeps Apify usage bounded.
- `DOWNLOAD_TTL_MINUTES=90` and `DOWNLOAD_MAX_TOTAL_MB=600` remove stale local media automatically.
- `APIFY_MAX_CHARGE_USD=0.05` can cap the cost of one Instagram actor run.
- `YTDLP_FORMAT` caps default downloads at 720p to reduce bandwidth, disk, and CPU while still allowing fallbacks.
- `YOUTUBE_CLIENTS` and `YOUTUBE_FALLBACK_CLIENTS` try multiple yt-dlp YouTube clients before giving up.
- Instagram carousel posts are downloaded as one batch and sent as Telegram albums when the media fits Telegram's album/upload rules.
- `TELEGRAM_SAFE_VIDEO_TRANSCODE=true` converts VP9/HEVC/odd Instagram MP4s to Telegram-safe H.264/AAC MP4 before upload.
- Progress messages use a compact live status card and are edited in place.
- `TELEGRAM_MESSAGE_EFFECT_ID` can attach a Telegram message effect in private chats when you provide a valid effect ID.
- `LOADER_STICKER_FILE_ID` or `LOADER_ANIMATION_FILE_ID` can show a Telegram-native animated loader during jobs.
- Bot-sent messages are tracked in `MESSAGE_LOG_PATH` so `/clean` can remove recent clutter.
- Completed downloads are stored in a per-request temp folder and deleted after sending.
- Failed requests are written to `FAILURE_LOG_PATH` as JSONL and to stdout for host logs.
- `LOG_FULL_URLS=false` keeps failure logs sanitized by default.

## YouTube notes

YouTube sometimes blocks cloud-hosted downloaders with "Sign in to confirm you're not a bot." Keep `yt-dlp` current first, then use these options only when needed:

- `YTDLP_COOKIES_B64` can hold a base64-encoded Netscape cookie file in host secrets. This is cheap, but cookies expire and should not be committed.
- `YOUTUBE_PO_PROVIDER=http` enables the installed bgutil PO-token plugin when a bgutil HTTP provider is reachable. Set `YOUTUBE_BGUTIL_BASE_URL` if it is not on the default local port.
- `YOUTUBE_PO_PROVIDER=script` enables bgutil script mode when the provider files and Node.js/Deno are installed. Set `YOUTUBE_BGUTIL_SERVER_HOME`.

Paid download APIs are usually wrappers around the same moving target, so they are the last resort rather than the first fix.

## Bot commands

- `/start` shows basic usage.
- `/status` shows uptime, disk, cleanup, and config limits without exposing tokens.
- `/whoami` shows your Telegram chat/user ID for `ADMIN_CHAT_IDS`.
- `/failures 10` shows recent failure logs for admins.
- `/failures clear` clears stale failure logs for admins.
- `/clean 30` removes recent bot-sent messages that are still deletable.
- `/loaderid` replies with a file ID when used as a reply to a sticker, GIF, photo, video, or file. `/fileid` and `/id` also work.
- `/demo` previews the live progress status without downloading media.

## Local run

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

## Docker run

```sh
docker compose up -d --build
```

## AWS EC2 deploy

This repo includes `deploy/tg-x-bot.service` for a small EC2/Lightsail Ubuntu server:

```sh
sudo apt-get update
sudo apt-get install -y ffmpeg python3-venv git
git clone https://github.com/mizu121/tg-x-bot.git /home/ubuntu/tg-x-bot
cd /home/ubuntu/tg-x-bot
cp .env.example .env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo cp deploy/tg-x-bot.service /etc/systemd/system/tg-x-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg-x-bot
```

## Render deploy

Use `render.yaml` as a Blueprint. Create a Background Worker, set `BOT_TOKEN`, `ADMIN_CHAT_IDS`, and optional `APIFY_TOKEN`, and keep the worker count at one to avoid duplicate Telegram polling. The Blueprint mounts a small persistent disk at `/var/data` for failure logs and temp download cleanup.

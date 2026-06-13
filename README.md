# Telegram X Video Downloader Bot

A Telegram polling bot that downloads videos from YouTube, X/Twitter, Instagram, and TikTok. It can also fetch recent Instagram reels with Apify when `APIFY_TOKEN` is configured.

## Required config

Copy `.env.example` to `.env` locally or set the same variables in your host:

```sh
BOT_TOKEN=your_telegram_bot_token
APIFY_TOKEN=optional_apify_token_for_instagram_reels
```

Do not commit `.env`, cookie files, Telegram tokens, Apify tokens, or Instagram session cookies.

## Cost and safety defaults

The bot defaults are intentionally conservative:

- `MAX_CONCURRENT_DOWNLOADS=1` prevents multiple large downloads from filling a small server.
- `MAX_UPLOAD_MB=49` avoids downloading files that Telegram is unlikely to accept.
- `MAX_REELS_PER_REQUEST=5` keeps Apify usage bounded.
- `YTDLP_FORMAT` caps default downloads at 720p to reduce bandwidth, disk, and CPU.
- Completed downloads are stored in a per-request temp folder and deleted after sending.

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

Use `render.yaml` as a Blueprint. Create a Background Worker, set `BOT_TOKEN` and optional `APIFY_TOKEN`, and keep the worker count at one to avoid duplicate Telegram polling.

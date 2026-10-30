# SNS Downloader

Self-hosted web app that downloads videos from TikTok, YouTube, Instagram,
Facebook and [any other site yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)
at the best available quality, without watermarks, and hands you a download
link. Everything is deleted automatically every night.

**Intended for a trusted LAN only. There is no login.** Do not expose it to the
internet without putting authentication in front of it.

## Run

```bash
mkdir -p data
docker compose up -d --build
```

Open <http://localhost:8080> (or `http://<server-ip>:8080` from another device).
Paste a URL, click **Download**, wait for the row to turn green, click the
**Download** link.

## Configuration

Set these in a `.env` file next to `docker-compose.yml` or in your shell.

| Variable | Default | Meaning |
|---|---|---|
| `TZ` | `UTC` | Timezone for the nightly cleanup |
| `CLEANUP_CRON` | `0 3 * * *` | When to wipe downloads (cron syntax) |
| `MAX_CONCURRENT` | `2` | Simultaneous downloads |
| `PUID` | `1000` | UID the container runs as; match the owner of `./data` |

## Instagram / Facebook private or login-walled posts

Public posts work without any setup. For content that needs a login:

1. Install a "Get cookies.txt" style browser extension (Netscape format).
2. While logged in to the site, export cookies for that site.
3. Save the file as `data/config/cookies.txt`.
4. Restart: `docker compose restart`.

The cookies file is only read when it exists. Cookies expire; if downloads
start failing with login errors, re-export it.

## Nightly cleanup

At `CLEANUP_CRON` (default 03:00 in `TZ`) all files under `data/downloads`
and `data/tmp` are deleted and the job list is emptied. You can trigger the
same thing at any time with the **Clear all** button or:

```bash
curl -X POST http://localhost:8080/api/cleanup
```

## Keeping yt-dlp current

Sites change often. If downloads start failing across the board, rebuild to
pull the latest yt-dlp:

```bash
make update
```

## Development

```bash
make venv     # create .venv and install deps
make test     # run the test suite (no network needed)
DATA_DIR=$PWD/data .venv/bin/uvicorn app.main:create_app --factory --reload --port 8080
```

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/jobs` | `{"url": "..."}` → job (201 new, 200 if already active, 422 invalid) |
| `GET` | `/api/jobs` | All jobs, newest first |
| `POST` | `/api/cleanup` | Delete all files and clear the list |
| `GET` | `/files/{filename}` | Download a finished file |

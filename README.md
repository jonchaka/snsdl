# SNS Downloader

Self-hosted web app that downloads videos from TikTok, YouTube, Instagram,
Facebook and [any other site yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)
at the quality you choose (defaulting to 1080p or best available), without
watermarks, and hands you a download link. Everything is deleted automatically every night.

**Intended for a trusted LAN only. There is no login.** Do not expose it to the
internet without putting authentication in front of it.

## Run

```bash
mkdir -p data
docker compose up -d --build
```

Open <http://localhost:8080> (or `http://<server-ip>:8080` from another device).
Paste a URL, click **Check**, pick Video (MP4) or Audio (MP3) and a quality,
click **Download**, wait for the row to turn green, then click the
**Download** link. Each row has a **Remove** button; on a queued, paused or
running job it reads **Cancel** and drops the job (stopping the download). Failed rows also get **Retry**;
queued rows get **Pause** and paused rows **Resume** (a resumed job rejoins the
end of the queue). The **Size** column shows each finished file's size, and the
footer shows the total space used by downloads and the yt-dlp version in use.

## Configuration

Set these in a `.env` file next to `docker-compose.yml` or in your shell.

| Variable | Default | Meaning |
|---|---|---|
| `TZ` | `UTC` | Timezone for the nightly cleanup |
| `CLEANUP_CRON` | `0 3 * * *` | When to wipe downloads (cron syntax) |
| `MAX_CONCURRENT` | `2` | Simultaneous downloads |
| `MAX_PLAYLIST_ITEMS` | `500` | Most videos one playlist download can queue |
| `PUID` | `1000` | UID the container runs as; match the owner of `./data`. Changing it needs `docker compose up -d --build` |
| `PORT` | `8080` | Host port to publish (compose only) |
| `DATA_DIR` | `/data` | Data directory inside the container; normally left alone |
| `COOKIES_FILE` | `/data/config/cookies.txt` | Cookies file inside the container; normally left alone |

## Instagram / Facebook private or login-walled posts

Public posts work without any setup. For content that needs a login:

1. Install a "Get cookies.txt" style browser extension (Netscape format).
2. While logged in to the site, export cookies for that site.
3. Save the file as `data/config/cookies.txt`.
4. Restart: `docker compose restart`.

Each download and probe works on its own temporary copy of the file, so the
exported cookies are never modified by yt-dlp.

The cookies file is only read when it exists. Cookies expire; if downloads
start failing with login errors, re-export it.

## Nightly cleanup

At `CLEANUP_CRON` (default 03:00 in `TZ`) all files under `data/downloads`
and `data/tmp` are deleted and the job list is emptied. You can trigger the
same thing at any time with the **Clear all** button or:

```bash
curl -X POST -H 'X-Requested-With: sns-downloader' http://localhost:8080/api/cleanup
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

## Choosing quality

Paste a URL and press **Check**. The app asks the site which resolutions it
offers and shows them in a dropdown, preselecting 1080p when available. Pick
**Audio (MP3)** instead to extract audio only, at best-quality VBR, 320 kbps
or 192 kbps. If the site stops offering the chosen resolution between Check
and Download, yt-dlp falls back to the next size down. Streams are kept in
the codec the site serves (no re-encoding); where a site offers several codecs
at the chosen resolution, AV1 is preferred for its smaller files, then VP9,
H.265 and H.264 in yt-dlp's default order.

Each row has a **Remove** button that deletes that file immediately instead of
waiting for the nightly cleanup. On a queued or downloading job the button
reads **Cancel**: it stops the download and discards any partial file.

## Playlists

If the URL is a playlist (or a YouTube video link that carries a `list=`
parameter), Check shows a **Download whole playlist** checkbox with the
number of videos and a **How many** field. Tick it to queue the first N
videos as separate rows, each with its own progress, Download link and
Cancel/Remove. The chosen format and quality apply to every video; videos
that lack that resolution fall back to the next size down. Leave the box
unticked to download just the single video. `MAX_PLAYLIST_ITEMS` (default
500) caps how many entries are read and queued. A line above the table
summarises each playlist that still has rows: **Cancel remaining** stops the
queued and downloading videos (finished ones stay), **Remove finished**
clears the done and failed rows of that playlist and deletes their files, and
**Retry failed** puts the failed ones back in the queue, and **Pause queued** /
**Resume paused** hold or release the videos that have not started yet. Any
failed row also has its own **Retry** button.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/probe` | `{"url": "..."}` → `{"title", "heights": [2160, 1080, ...], "sizes": [{"height", "width"}], "playlist": null \| {"title", "count", "total"}}`. Lists the video resolutions available. 422 with a message if the site refuses. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs` | `{"url": "...", "format": "mp4"\|"mp3", "quality": "..."}` → job (201 new, 200 if the same url+format+quality is already active, 422 invalid). `format` defaults to `mp4`, `quality` to `best`. For `mp4`, `quality` is a height such as `1080`; for `mp3` it is `best`, `320` or `192`. |
| `POST` | `/api/playlists` | `{"url": "...", "format": "...", "quality": "...", "limit": N}` → `{"playlist", "total", "queued", "reused", "jobs": [...]}` (201). Queues one job per playlist entry, at most `limit` and `MAX_PLAYLIST_ITEMS`; entries already active with the same format and quality are reused. 422 with a message if the URL is not a playlist, the site refuses, or the body is invalid; 403 without the header. |
| `DELETE` | `/api/playlists/{playlist_id}?status=active\|finished` | `status=active` (default): cancel every queued, paused or downloading job created by that playlist request → `{"cancelled": n}`; finished rows stay. `status=finished`: remove every done or failed row of that playlist and delete its file → `{"removed": n}`. 404 unknown id, 422 other status values. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/retry` | Put one failed job back in the queue (same id and row) → the job (404 unknown, 409 if not failed). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/playlists/{playlist_id}/retry` | Re-queue every failed job of that playlist request → `{"retried": n}` (404 unknown id). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/pause` | Hold a queued job back (status `paused`) → the job (404 unknown, 409 if not queued). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/resume` | Put a paused job back in the queue, at the end → the job (404 unknown, 409 if not paused). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/playlists/{playlist_id}/pause` | Pause every queued job of that playlist request → `{"paused": n}` (404 unknown id). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/playlists/{playlist_id}/resume` | Resume every paused job of that playlist request → `{"resumed": n}` (404 unknown id). Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/api/jobs` | All jobs, newest first |
| `DELETE` | `/api/jobs/{id}` | Remove one job and its file; on a queued or downloading job this cancels it (404 unknown). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/cleanup` | Delete all files and clear the list. Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/api/status` | `{"yt_dlp_version", "downloads": {"files", "bytes"}}` — shown in the page footer |
| `GET` | `/files/{filename}` | Download a finished file |

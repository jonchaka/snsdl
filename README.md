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
Paste a URL, click **Check**, pick Video (MP4), Audio (MP3) or Audio (original) and a quality,
click **Download**, wait for the row to turn green, then click the
**Download** link. Each row has a **Remove** button; on a queued, paused or
running job it reads **Cancel** and drops the job (stopping the download). Failed rows also get **Retry**;
queued rows get **Pause** and paused rows **Resume** (a resumed job rejoins the
end of the queue). While a video downloads, the Progress column shows the
current speed and time remaining. **Keep** on a finished row exempts that file
from the nightly cleanup and from **Clear all** (a `kept` badge shows; **Unkeep**
reverses it). The **Size** column shows each finished file's size, and the
footer shows the total space used by downloads and the yt-dlp version in use.
**Pause queue** in the footer stops any new download from starting until you
press **Resume queue**; downloads already running finish, and the setting
survives a restart. **Next** on a queued row moves it to the front of the queue
(a `next` badge shows until it starts; pausing it drops the badge, and a resumed
job rejoins the end). A failed row has a **Details** button that shows the last
lines yt-dlp printed, with temporary paths and cookie file names blanked out.

The page remembers the last format, quality and subtitle language you
downloaded with and preselects them on the next Check (the nearest lower
resolution if the exact one is not offered). The choice lives in your
browser's local storage, not on the server, so each browser has its own.

Downloads need free disk space: when the download disk has less than
`MIN_FREE_SPACE` (default 1 GB) free, a yellow banner says so, new downloads,
playlists and retries are refused with a message, and jobs already queued wait
(re-checking every 10 seconds) until space is freed, for example by removing
files or pressing **Clear all**. The footer always shows the free space. A
download that fills the disk while running fails with yt-dlp's own error.

## Sending links from your phone or browser

Opening `http://<server-ip>:8080/?url=<video link>` fills the box and runs
**Check** straight away, so one tap gets you to the quality choice. Ways to use it:

- **Bookmarklet** (desktop browser): create a bookmark whose address is
  `javascript:void(location.href='http://<server-ip>:8080/?url='+encodeURIComponent(location.href))`
  and click it while viewing a video page.
- **iOS Shortcuts**: a shortcut that accepts URLs from the share sheet and runs
  "URL Encode" on the input, then "Open URL" on `http://<server-ip>:8080/?url=[Encoded Text]`.
- **Android**: any share-to-URL app (for example "HTTP Shortcuts" or "URL Forwarder")
  pointed at the same address.

## Configuration

Set these in a `.env` file next to `docker-compose.yml` or in your shell.

| Variable | Default | Meaning |
|---|---|---|
| `TZ` | `UTC` | Timezone for the nightly cleanup |
| `CLEANUP_CRON` | `0 3 * * *` | When to wipe downloads (cron syntax) |
| `MAX_CONCURRENT` | `2` | Simultaneous downloads |
| `MAX_PLAYLIST_ITEMS` | `500` | Most videos one playlist download can queue |
| `EMBED_THUMBNAILS` | `1` | Embed the video thumbnail as cover art in MP4 and MP3 files; set `0` to skip |
| `UPDATE_CRON` | `0 4 * * *` | When to install the newest yt-dlp (cron syntax); empty disables it |
| `IMPERSONATE` | `chrome` | Browser whose TLS/HTTP fingerprint yt-dlp imitates on every request (`chrome`, `safari`, `chrome-120:windows-10`…); empty disables. Shown in the footer |
| `RATE_LIMIT` | *(none)* | Cap each download's speed in bytes per second: `5M` (5 MiB/s), `800K`, `1.5M` or a plain number; shown in the footer |
| `MIN_FREE_SPACE` | `1G` | Free space the download disk must keep, same syntax as `RATE_LIMIT`. Below it new downloads are refused and queued ones wait; `0` disables the check |
| `PROXY` | *(none)* | Send probes and downloads through a proxy: an `http`, `https`, `socks4`, `socks4a`, `socks5` or `socks5h` URL with a host, optionally with `user:password@`; empty disables. The footer shows the host, never the password. Thumbnail previews are skipped under a SOCKS proxy |
| `FRAGMENTS` | `1` | Fragments of an HLS/DASH stream one download fetches at the same time (1–16); higher can speed up sites that serve video in pieces |
| `PUID` | `1000` | UID the container runs as; match the owner of `./data`. Changing it needs `docker compose up -d --build` |
| `PORT` | `8080` | Host port to publish (compose only) |
| `DATA_DIR` | `/data` | Data directory inside the container; normally left alone |
| `COOKIES_FILE` | `/data/config/cookies.txt` | Cookies file inside the container; normally left alone |

## Instagram / Facebook private or login-walled posts

Public posts work without any setup. When a video needs a login (private or
followers-only Instagram and Facebook posts, members-only videos), the Check
message or the failed row says so and offers **Add cookies** and **Learn how**.
Cookies are the small tokens that keep you signed in; the app reuses your
browser's login by importing them.

1. **Learn how** shows the steps: install a cookies.txt exporter in your browser
   ("Get cookies.txt LOCALLY" for Chrome/Edge/Brave, "cookies.txt" for Firefox),
   log in to the site, export for that site in Netscape format.
2. **Add cookies**: pick the exported file or paste its contents, then choose
   **Use once for this download, then discard** (default) or **Keep for future
   downloads**. Saving re-queues the failed download at once; from the Check
   message it re-runs Check.
3. Kept cookies appear under **Cookies (n)** at the top of the page with the
   sites they cover, the cookie count, the date added and the expiry, and a
   **Delete** button each. They are used automatically for every download and
   probe. Use-once cookies are deleted as soon as that download finishes,
   fails or is cancelled.

Cookies are a login: anyone who can open this page on your network can download
private content with the kept ones, and they are stored unencrypted (owner-only
file permissions) under `data/config/cookies/`. Prefer use-once, and consider a
throwaway account for Instagram and Facebook. Cookies expire; the panel flags an
entry as expired when none of its cookies is still valid.

The old manual route still works: save an export as `data/config/cookies.txt`
and it is listed read-only in the same panel. Every download and probe works on
its own temporary copy, so exported files are never modified by yt-dlp.

Downloads and probes present a real browser's TLS and HTTP fingerprint
(`IMPERSONATE`, default `chrome`, via the `curl_cffi` backend), which is what
Instagram and Facebook check most. Because the app runs on your own network, the
requests also come from the same address as your browser, so there is no
location mismatch for the site to notice.

## Nightly cleanup

At `CLEANUP_CRON` (default 03:00 in `TZ`) all files under `data/downloads`
and `data/tmp` are deleted and the job list is emptied, except for rows marked
**Keep**, which stay until you press **Unkeep** or **Remove**. You can trigger the
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
or 192 kbps, or **Audio (original)** to keep the site's best audio stream
as served, without re-encoding (the file is `.m4a`, `.opus` or `.ogg`
depending on the codec; a source that is already `.mp3`, `.flac` or `.wav`
is kept as is, and the few codecs with no matching container are converted
to MP3; cover art is skipped where the container cannot hold it). If the site stops offering the chosen resolution between Check
and Download, yt-dlp falls back to the next size down. Streams are kept in
the codec the site serves (no re-encoding); where a site offers several codecs
at the chosen resolution, AV1 is preferred for its smaller files, then VP9,
H.265 and H.264 in yt-dlp's default order. The video's thumbnail is embedded
as cover art in MP4, MP3 and original-audio files (`EMBED_THUMBNAILS=0` turns this off).

Where the site reports file sizes, each quality shows an estimate
(`1080p · ~180 MB`); the estimate includes the audio track and is missing when
the site does not say. Check also shows the video's length and its thumbnail.
Thumbnails are fetched by the server (only from addresses yt-dlp reports, at
most 2 MiB, JPEG, PNG, WebP or GIF), stored in `data/thumbs` and deleted with
their row, by **Clear all** and by the nightly cleanup. They are skipped when
`PROXY` is a SOCKS proxy.

To download only part of a video, fill in **Start** and/or **End** under
**Clip** (`90`, `1:30` or `1:02:03`; an empty Start means the beginning, an
empty End the end of the video). The cut is made at the nearest keyframe
without re-encoding, so a clip may start a few seconds before the time you
entered. Clipping works for MP4, MP3 and original audio; the row shows the
range and "Clipping…" while it runs, since ffmpeg reports no percentage. The
Clip row is hidden when the site does not report the video's length, for
playlists and when several links are pasted.

If the site offers subtitles, a **Subtitles** dropdown appears for MP4
downloads listing the site's own tracks and, where available, the automatic
captions in the video's own language (marked "auto"). The chosen track is
embedded in the MP4 as a soft subtitle stream you can switch on in your player.
For a playlist the choice applies to every video; a video without that
language simply downloads without subtitles. If the site refuses the subtitle
file itself (YouTube occasionally rate-limits caption requests), the job
fails like any other download error; **Retry** it, or retry without subtitles.

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
summarises each playlist that still has rows, with these buttons:

- **Cancel remaining** stops the queued, paused and downloading videos; finished ones stay.
- **Remove finished** clears the done and failed rows of that playlist and deletes
  their files (rows marked **Keep** are left alone).
- **Retry failed** puts the failed ones back in the queue. Any failed row also has
  its own **Retry** button.
- **Pause queued** / **Resume paused** hold or release the videos that have not started yet.
- **Download all** fetches every finished video of the playlist as one zip; files
  are stored uncompressed and named by playlist position (`001-Title.mp4`).

## Several links at once

Paste several links into the box (one per line or separated by spaces;
Shift+Enter adds a new line, Enter submits) and press **Check**. The page skips
the per-video check and offers the format and a generic quality list ("Best
available", "Up to 1080p"…); **Download** queues one ordinary row per link.
Duplicate links are queued once, links already queued with the same format and
quality are reused, and anything that is not an `http://` or `https://` link is
listed as skipped. `MAX_PLAYLIST_ITEMS` caps how many links one paste can
queue. Pasting into the empty box runs **Check** straight away.

## Keeping yt-dlp up to date

Sites change often and the fix is almost always a newer yt-dlp. Every night at
`UPDATE_CRON` (04:00 by default, after the 03:00 cleanup) the app runs
`pip install --upgrade yt-dlp` into `data/lib`, which is on the container's
`PYTHONPATH`, so the newer copy shadows the one baked into the image and
survives `docker compose up --build`. If the installed version differs from
the running one, the app stops starting new downloads, waits for running ones
to finish, and exits; Docker's `restart: unless-stopped` policy brings it back
on the new version within seconds. The footer shows the running version and
"ready, restarting when idle" while that is in progress. **Update yt-dlp** in
the footer runs the same update immediately.

Notes:

- The self-restart relies on `restart: unless-stopped` (set in the compose
  file). Without a restart policy the container would simply stop.
- The new copy is downloaded into `data/lib.staging`, import-tested, and only
  swapped into `data/lib` at the idle restart, so the running process never has
  its files replaced underneath it. On every start the container checks that
  `data/lib` imports; a broken copy is moved to `data/lib.broken` and the
  version baked into the image is used. Deleting `data/lib` by hand also falls
  back to the image version.
- The copy in `data/lib` shadows the image's yt-dlp even after an image
  rebuild, until the next nightly update replaces it.
- Only yt-dlp itself is updated (`pip --no-deps`); its optional helper
  packages come from the image. Rebuild the image occasionally to refresh
  those.

## Testing

`make test` runs the unit and API tests (no network, no browser). `make
browser-test` installs Chromium for Playwright (about 170 MB, once) and drives
the real page through the Check, Download, Pause/Resume, Retry, Cancel,
playlist and queue-pause flows with a faked yt-dlp. Those browser tests skip
automatically when Playwright or Chromium is not installed.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/cookies` | `{"entries": [{"id", "source": "kept"\|"legacy", "sites", "count", "added_at", "expires_at", "expired"}]}` — metadata only, never cookie values. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/cookies` | `{"text": "<Netscape cookies.txt>", "keep": bool, "job_id": optional}` → `{"entry", "retried"}` (201). `keep=false` needs `job_id` and is used by that job only; a failed `job_id` is re-queued. 422 with a reason if the text is not a cookies file; 404 unknown job. Needs the header. |
| `DELETE` | `/api/cookies/{id}` | Delete a kept entry (204; 404 unknown or legacy). Needs the header. |
| `POST` | `/api/probe` | `{"url": "..."}` → `{"title", "duration", "heights": [2160, 1080, ...], "sizes": [{"height", "width", "bytes"}], "audio_bytes", "subtitles": [{"lang", "name", "auto"}], "thumb", "playlist": null \| {"title", "count", "total"}}`. Lists the video resolutions available. `duration` is whole seconds (`null` for live streams or when unknown); `sizes[].bytes` is the estimated file size at that height including audio, and `audio_bytes` that of the best audio-only stream (`null` when the site does not say); `thumb` is a thumbnail key for `/api/thumbs/{key}` or `null`. 422 `{"detail", "needs_login"}` if the site refuses (`needs_login` true when it wants a logged-in session). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs` | `{"url": "...", "format": "mp4"\|"mp3"\|"audio", "quality": "..."}` → job (201 new, 200 if the same url+format+quality+subtitles is already active, 422 invalid). `format` defaults to `mp4`, `quality` to `best`. For `mp4`, `quality` is a height such as `1080`; for `mp3` it is `best`, `320` or `192`; for `audio` (best stream, no re-encode) it must be `best`. Optional `"subtitles": "en"` or `"auto:en"` (mp4 only; a `lang` from the probe) embeds that track. Optional `"clip_start"` / `"clip_end"` (seconds, 0–9999999, end after start; either may be left out) download only that range; different clips of one video are separate jobs. Optional `"thumb"`: the key `/api/probe` returned (422 if unknown). 507 `{"detail": "Only 600.0 MB free on the download disk; downloads need at least 1.0 GB"}` when free space is below `MIN_FREE_SPACE`; the same 507 applies to batch, playlists, both retry routes and cookies that would re-queue a failed job. |
| `POST` | `/api/jobs/batch` | `{"urls": ["...", ...], "format": "...", "quality": "..."}` (1 to 2000 entries; same format and quality rules as `/api/jobs`) → `{"queued", "reused", "rejected": ["..."], "jobs": [...]}` (201). Links are deduplicated in order, invalid ones listed in `rejected` and skipped, active jobs with the same url, format and quality reused. 422 if no link is valid or the distinct valid links exceed `MAX_PLAYLIST_ITEMS`; 507 low disk. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/playlists` | `{"url": "...", "format": "...", "quality": "...", "limit": N, "subtitles": optional}` → `{"playlist", "total", "queued", "reused", "jobs": [...]}` (201). Queues one job per playlist entry, at most `limit` and `MAX_PLAYLIST_ITEMS`; entries already active with the same format, quality and subtitles are reused. 422 with a message if the URL is not a playlist, the site refuses, or the body is invalid (clips apply to single videos only); 507 low disk; 403 without the header. |
| `DELETE` | `/api/playlists/{playlist_id}?status=active\|finished` | `status=active` (default): cancel every queued, paused or downloading job created by that playlist request → `{"cancelled": n}`; finished rows stay. `status=finished`: remove every done or failed row of that playlist (kept rows excepted) and delete its file → `{"removed": n}`. 404 unknown id, 422 other status values. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/retry` | Put one failed job back in the queue (same id and row) → the job (404 unknown, 409 if not failed). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/playlists/{playlist_id}/retry` | Re-queue every failed job of that playlist request → `{"retried": n}` (404 unknown id). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/keep` | Exempt a finished download from the nightly cleanup and Clear all → the job (404 unknown, 409 if not done). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/unkeep` | Undo keep → the job (404 unknown, 409 if not done). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/pause` | Hold a queued job back (status `paused`) → the job (404 unknown, 409 if not queued). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/next` | Download a queued job next: moves it to the front of the queue and sets `priority` → the job (404 unknown, 409 if not queued). Pausing the job clears `priority`. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/resume` | Put a paused job back in the queue, at the end → the job (404 unknown, 409 if not paused). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/playlists/{playlist_id}/pause` | Pause every queued job of that playlist request → `{"paused": n}` (404 unknown id). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/playlists/{playlist_id}/resume` | Resume every paused job of that playlist request → `{"resumed": n}` (404 unknown id). Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/api/playlists/{playlist_id}/zip` | Stream a zip (stored, not compressed) of that playlist's finished files, named `NNN-title.ext` by playlist position (404 unknown id or nothing finished yet) |
| `GET` | `/api/jobs` | All jobs, newest first (failed jobs carry `needs_login` when cookies may help). Jobs carry `clip_start`, `clip_end`, `priority` and `thumb`; the failure log is not included |
| `GET` | `/api/jobs/{id}/log` | `{"log": "..."}`: the last yt-dlp lines of a failed download (at most 8 KB, temporary paths and cookie file names blanked), `""` when there is none (404 unknown) |
| `GET` | `/api/thumbs/{key}` | A stored thumbnail image, cached for a day (404 unknown) |
| `DELETE` | `/api/jobs/{id}` | Remove one job and its file; on a queued or downloading job this cancels it (404 unknown). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/cleanup` | Delete all files and clear the list, except kept rows. Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/api/status` | `{"yt_dlp_version", "downloads": {"files", "bytes"}, "queue_paused", "rate_limit", "impersonate": {"target", "active"}, "update": {...}, "disk": {"free", "min_free", "low"}, "proxy"}` — shown in the page footer. `disk` is in bytes (`low` true below `MIN_FREE_SPACE`); `proxy` is the proxy URL without credentials, or `null` |
| `POST` | `/api/queue/pause` | Stop starting new downloads (running ones finish); persists across restarts → `{"queue_paused": true}`. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/queue/resume` | Start taking jobs again → `{"queue_paused": false}`. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/update` | Install the newest yt-dlp into `data/lib` now → `{"running", "installed", "restart_pending", "last_checked", "error", "note"}`; restarts when idle if the version changed. Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/files/{filename}` | Download a finished file |

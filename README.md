# SNS Downloader

[![CI](https://github.com/jonchaka/snsdl/actions/workflows/ci.yml/badge.svg)](https://github.com/jonchaka/snsdl/actions/workflows/ci.yml)

Self-hosted web app that downloads videos from TikTok, YouTube, Instagram,
Facebook and [any other site yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)
at the quality you choose (defaulting to 1080p or best available), without
watermarks, and hands you a download link. Everything is deleted automatically every night
(or, with [`RETENTION_DAYS`](#retention), a set number of days after it was downloaded).

**Intended for a trusted LAN only.** There are no user accounts; an optional
[access PIN](#access-pin) keeps casual visitors out. Do not expose it to the
internet without putting HTTPS and real authentication in front of it.

## Install

A ready-built image is published as `ghcr.io/jonchaka/snsdl` (amd64 and arm64) on
every green push to `main`, and rebuilt from scratch every Monday so it always carries a
recent yt-dlp. Releases are also tagged: `ghcr.io/jonchaka/snsdl:1.0` always points at the
newest 1.0.x build, `1.0.0` at exactly that release; `latest` follows `main`. See
[CHANGELOG.md](CHANGELOG.md). Pick one of:

**Portainer.** Stacks → Add stack → name `snsdl` → **Repository**: URL
`https://github.com/jonchaka/snsdl`, reference `refs/heads/main`, compose path
`docker-compose.yml`. Under *Environment variables* add `DATA_PATH` (an absolute host
folder, for example `/volume1/docker/snsdl/data`), `PUID` and `PGID` (the owner of that
folder: `id -u` and `id -g` over SSH), and `PORT` or `TZ` if you want to change them.
Deploy. Updating is **Pull and redeploy** on the stack. (Or choose **Web editor** and paste
the contents of `docker-compose.yml`.)

**Docker Compose with the image.**

```bash
mkdir snsdl && cd snsdl
curl -O https://raw.githubusercontent.com/jonchaka/snsdl/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/jonchaka/snsdl/main/.env.example   # optional: edit to taste
docker compose up -d
```

Updating: `docker compose pull && docker compose up -d`.

**From source.**

```bash
git clone https://github.com/jonchaka/snsdl.git && cd snsdl
make up          # builds the image locally (docker-compose.build.yml) and starts it
```

Updating: `git pull && make update`. Settings go in `.env` (see
[Configuration](#configuration)); `PORT=8180 make up` style overrides work too.

### File ownership

The container starts as root, makes the data folder owned by `PUID:PGID` (and the
`KEEP_DIR` folder itself, not the files inside it: a media library stays as it is), then
runs as that user. The folder is created on first start, so `mkdir` is not needed, and
changing `PUID` later needs no rebuild. On a network share where the server fixes
ownership (CIFS `uid=`, NFS with root squash) the re-owning is skipped with a warning and
the folder only has to be writable. If you set `user:` in the compose file instead,
nothing is re-owned and the folder must already be writable by that user. When it is not,
the container stops with one line in its log:

```
/data is not writable by uid 1000. The volume mounted at /data must be writable: check the host path exists and is not read-only, or set PUID/PGID to the owner of that folder (id -u / id -g)
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
footer shows the build version, the total space used by downloads and the yt-dlp version in use.
**Pause queue** in the footer stops any new download from starting until you
press **Resume queue**; downloads already running finish, and the setting
survives a restart. **Next** on a queued row moves it to the front of the queue
(a `next` badge shows until it starts; pausing it drops the badge, and a resumed
job rejoins the end). A failed row has a **Details** button that shows the last
lines yt-dlp printed, with temporary paths and cookie file names blanked out.

**Remove** on a row (and the bulk **Remove** below) hides the row at
once and shows a `Removed <title> · Undo` message for 5 seconds; the file is
only deleted when the message expires, so **Undo** brings the row straight
back. The delay lives in the page: closing the tab within those 5 seconds
leaves the download in place. **Remove finished** on a playlist still asks
for confirmation instead.

**QR** on a finished row shows a QR code of the file's download link, with the
link written underneath: scan it with a phone on the same network to download
the file there. The link uses the address your browser used to reach the app,
so open the page by its LAN address (not `localhost`) before showing the code.

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
- **Android**: with [HTTPS on and the app installed](#https-and-installing-the-app-on-your-phone),
  the app itself appears in the share sheet; otherwise any share-to-URL app (for example "HTTP Shortcuts" or "URL Forwarder")
  pointed at the same address.

### Search

Typing words instead of a link (anything without `://` that contains a space
or no dot, such as `lofi beats`) and pressing **Check** searches YouTube and
lists the first 8 results under the box with thumbnail, title, uploader and
length. **Use** fills the box with that video's link and runs Check; **Clear**
hides the list. Search is YouTube only. The several-links mode and
`/?url=` links never search, and words are never queued as a download.
Result thumbnails are fetched by the server from the addresses yt-dlp reports
(3 seconds each at most; a slow one is left blank); since no row refers to
them, the orphan sweep (at startup and in the nightly cleanup, for files older
than an hour) removes them again.

## Filtering and bulk actions

The **Filter downloads** box above the list narrows it as you type, matching
the title, link, site, status and format (case does not matter). A playlist's
summary line stays while any of its videos matches. Each row has a checkbox;
once one is ticked a bar shows the number selected and **Select all shown**,
**Keep**, **Unkeep**, **Remove**, **Download all** and **Clear selection**.
Keep and Unkeep apply to the finished downloads among the selection (others
are skipped), Remove works like each row's Remove/Cancel (with Undo), and
**Download all** fetches the selection's finished files as one zip. The
selection survives the list refreshing.

## Configuration

Set these in a `.env` file next to `docker-compose.yml` or in your shell.
`.env.example` lists them all with their defaults: copy it to `.env` and
uncomment what you change.

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
| `MAX_PER_SITE` | `1` | Downloads from one site that may run at the same time (see [pacing](#per-site-pacing-and-automatic-retry)); `0` means no per-site limit, only `MAX_CONCURRENT` |
| `SITE_GAP` | `3` | Seconds to pause between two downloads from the same site, varied randomly between half and one and a half times this; `0` disables the pause |
| `KEEP_DIR` | *(none)* | Directory inside the container that **Keep** moves files into, out of the cleanup's reach (see [kept files](#kept-files-directory)); empty keeps them in `data/downloads`. Must not be (or be inside) `data/downloads` or `data/tmp`, which the cleanup empties; the app refuses to start if it is |
| `ACCESS_PIN` | *(none)* | PIN (4–64 characters, no spaces) the page asks for once per browser before showing anything (see [access PIN](#access-pin)); empty leaves the app open |
| `HTTPS` | `0` | Serve the page over HTTPS with a certificate the app generates itself (see [HTTPS](#https-and-installing-the-app-on-your-phone)); `1`/`true`/`yes`/`on` enables it, same port |
| `TLS_HOSTS` | *(none)* | Comma-separated addresses and names you open the page by, such as `192.168.1.10,nas.local`; the certificate covers them plus `localhost`, `127.0.0.1`, `::1` and the container's hostname. Each must be an IP address or a plain hostname (letters, digits, `-` and dots); the app refuses to start otherwise |
| `FRAGMENTS` | `1` | Fragments of an HLS/DASH stream one download fetches at the same time (1–16); higher can speed up sites that serve video in pieces |
| `SPONSORBLOCK` | `0` | Page default for **Remove sponsor segments** on YouTube videos (see [SponsorBlock](#choosing-quality)); `1` ticks it the first time the page opens, after that the page remembers your choice |
| `PREFER_H264` | `0` | Page default for **Prefer H.264** on MP4 downloads (see [Choosing quality](#choosing-quality)); `1` ticks it the first time, then the page remembers |
| `DOWNLOAD_WINDOW` | *(none)* | Only start downloads between two times in `TZ`, `HH:MM-HH:MM` (24 h, may wrap past midnight like `22:00-06:00`); empty means always (see [download window](#download-window)) |
| `WEBHOOK_URL` | *(none)* | `http://` or `https://` URL (with a host) that gets a JSON POST when a download finishes or fails (see [webhook](#webhook)); empty disables |
| `WEBHOOK_SECRET` | *(none)* | Key for the webhook's `X-SNS-Signature` HMAC; empty sends unsigned. Never logged or shown |
| `PUBLIC_URL` | *(none)* | The address you open the app by, as an origin such as `https://192.168.1.10:8080` (no path); used for the absolute `file_url` in the webhook, the links in [Telegram](#telegram) messages and as the base of [podcast feed](#podcast-feed) links. Empty sends a relative `/files/...` path in the webhook, and feeds use the address the podcast app connected to |
| `TELEGRAM_BOT_TOKEN` | *(none)* | Token of the Telegram bot that sends finished downloads to `TELEGRAM_CHAT_ID` (see [Telegram](#telegram)), as BotFather gives it (`123456789:AA...`); needs `TELEGRAM_CHAT_ID`, the app refuses to start with only one of the two. Never logged or shown |
| `TELEGRAM_CHAT_ID` | *(none)* | The chat the bot sends to: a numeric id (`123456789`, or `-100...` for a group or channel) or a public channel's `@name`; needs `TELEGRAM_BOT_TOKEN` |
| `RETENTION_DAYS` | *(none)* | Instead of wiping everything at `CLEANUP_CRON`, delete each finished or failed download this many days after it finished (see [retention](#retention)); a whole number of 1 or more, empty or `0` keeps the nightly wipe |
| `LIVE_MAX_HOURS` | `12` | Longest a [live recording](#live-streams) may run before it is stopped and kept (1–168 hours) |
| `MEDIA_LAYOUT` | `0` | `1` files kept downloads into one folder per uploader under `KEEP_DIR`, with a `.nfo` and a poster image for Jellyfin, Plex or Kodi (see [media server layout](#media-server-layout)); needs `KEEP_DIR`, the app refuses to start without it |
| `SUBSCRIPTION_CRON` | `0 */6 * * *` | When to check [subscriptions](#subscriptions) for new videos (cron syntax, in `TZ`); empty means manual checks only (**Check now**) |
| `PUID` | `1000` | UID the app runs as. At start the container re-owns the data folder (and the `KEEP_DIR` folder itself) to `PUID:PGID` and drops to that user; change it any time, no rebuild |
| `PGID` | *(same as `PUID`)* | Group id the app runs as (see `PUID`); blank or unset means the same as `PUID` |
| `PORT` | `8080` | Host port to publish (compose and Portainer) |
| `DATA_PATH` | `./data` | Host folder mounted as `/data` (compose and Portainer); Portainer stacks should use an absolute path |
| `KEEP_PATH` | `./keep` | Host folder for the optional keep volume (see [kept files](#kept-files-directory)) |
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

Downloads run one at a time per site with a short pause in between (see
[pacing](#per-site-pacing-and-automatic-retry)), so a playlist or a long paste does
not hammer a site with the account the cookies belong to.

Downloads and probes present a real browser's TLS and HTTP fingerprint
(`IMPERSONATE`, default `chrome`, via the `curl_cffi` backend), which is what
Instagram and Facebook check most. Because the app runs on your own network, the
requests also come from the same address as your browser, so there is no
location mismatch for the site to notice.

## Per-site pacing and automatic retry

By default only one download per site runs at a time (`MAX_PER_SITE=1`), and
the next download from that site starts about 3 seconds after the previous one
ended (`SITE_GAP=3`, varied randomly between half and one and a half times the
value). Downloads from other sites are not held up: with `MAX_CONCURRENT=2`,
a TikTok and a YouTube video still run side by side, while a 50-video playlist
from one site goes one after another. A burst of parallel requests from one
address is what sites flag as a bot, and when those requests carry your cookies
it is your account that gets challenged or locked; pacing makes the traffic
look like one person watching. `www.`, `m.`, `vm.`, `vt.` and `mobile.` count as
the same site, as does `youtu.be` for `youtube.com`. Set `MAX_PER_SITE=0` and
`SITE_GAP=0` for the old behaviour. The footer shows the pacing when it differs
from the defaults.

A download that fails with a network-type error (a timeout, a reset or refused
connection, a DNS failure, HTTP 429 or a 5xx from the site) is retried once,
automatically, 30 seconds later. Meanwhile the row reads
`Retrying in 24 s · <message>` and can be paused or cancelled like any queued
job. Login and other errors are not retried, and a second failure leaves the
row failed with its **Retry** button as before. Use-once cookies are discarded
after the first attempt, so the automatic retry runs without them; if it then
fails for want of a login, add them again from the failed row.

## Nightly cleanup

At `CLEANUP_CRON` (default 03:00 in `TZ`) all files under `data/downloads`
and `data/tmp` are deleted and the job list is emptied, except for rows marked
**Keep**, which stay until you press **Unkeep** or **Remove**, and jobs that are
still queued, paused or downloading (so a subscription's or the download
window's waiting jobs survive the night). Separate subtitle files go with
their video. You can clear everything (waiting jobs included, kept rows
excepted) at any time with the **Clear all** button or:

```bash
curl -X POST -H 'X-Requested-With: sns-downloader' http://localhost:8080/api/cleanup
```

### Retention

Set `RETENTION_DAYS` (for example `7`) to keep downloads around for a while
instead of wiping them every night. The run at `CLEANUP_CRON` then removes
only the done and failed rows that finished more than that many days ago
(rows from before this version count from when they were added), with their
files, subtitle files and thumbnails; files in `data/downloads` that no row
refers to are deleted once they are older than the cutoff, and `data/tmp` is
emptied as before. Kept rows and queued, paused and downloading jobs are never
touched, and the [history](#history) stays. The footer reads `Files are
deleted 7 days after download unless kept.` **Clear all** is unchanged: it
still clears everything except kept rows.

### Kept files directory

Kept rows normally stay in `data/downloads` next to everything else. Set
`KEEP_DIR` to give them their own directory, for example a media library
folder: **Keep** then moves the file there and **Unkeep** moves it back (a
name already taken gets ` (2)`, ` (3)`… before the extension). The cleanup
never looks inside `KEEP_DIR`, and download links, zips and QR codes keep
working. Mount the directory into the container by uncommenting the volume
line in `docker-compose.yml` (any host path works on the left):

```yaml
    volumes:
      - ${DATA_PATH:-./data}:/data
      - ${KEEP_PATH:-./keep}:/keep
```

then point `KEEP_DIR` at the mount in `.env` and run `docker compose up -d`:

```bash
KEEP_DIR=/keep
```

The directory itself is re-owned to
`PUID:PGID` when the container starts (files inside it are left alone) (see [File ownership](#file-ownership)). **Remove** deletes a kept file from
`KEEP_DIR` too. The footer's space total includes it. A row kept before
`KEEP_DIR` was set stays in `data/downloads`; press **Unkeep** and **Keep**
again to move it.

### Media server layout

With `MEDIA_LAYOUT=1` (it needs `KEEP_DIR`), **Keep** files a download the
way media servers expect: the video and its subtitle files go into a folder
named after the uploader (`KEEP_DIR/<uploader>/`; characters that are not
allowed in folder names become spaces, at most 80 characters, `Other` when
the site names nobody), next to a `<name>.nfo` file (title, description,
upload date, uploader, length and the source URL) and a `<name>-poster.jpg`
(or `.png`/`.webp`) copy of the thumbnail. Point a Jellyfin, Plex or Kodi
library at `KEEP_DIR` (Jellyfin and Kodi read the `.nfo`; Plex ignores it
without a third-party agent but picks up the poster; choose a "Movies" or "Home videos" style library, as
these are single files, not TV episodes). The `.nfo` and poster are a best
effort: if writing them fails the keep still succeeds. **Unkeep** moves the
video back to `data/downloads`, deletes the `.nfo` and poster and removes the
folder once it is empty; **Remove** deletes all of it. Download links, QR
codes, zips and feeds use `/files/<uploader>/<name>` for such files. Rows kept
before `MEDIA_LAYOUT` was turned on stay where they are; Unkeep and Keep
them again to move them into a folder.

**Keep automatically.** With `KEEP_DIR` set, a subscription can be added with
**keep automatically**: every download it queues is kept (and, with
`MEDIA_LAYOUT`, filed into its uploader's folder) as soon as it finishes. If
the move fails the row stays done and unkept, and the log says why.

## Access PIN

Set `ACCESS_PIN` (4–64 characters, no spaces) to make the page ask for it
before showing anything. A correct PIN is remembered by that browser for a
year in an `HttpOnly` cookie, so each browser asks once; **Lock** in the
footer makes that browser forget it. The cookie holds an HMAC of the PIN made
with a random per-install key in `data/config/secret`, never the PIN itself:
changing the PIN or deleting that file signs every browser out. That is also
the only way to revoke a cookie: **Lock** clears it from one browser, but a
copy of the cookie taken earlier stays valid until the PIN or the secret file
changes. The cookie is marked `Secure` only when the app itself sees an HTTPS
request; behind a reverse proxy that terminates TLS the app sees plain HTTP,
so have the proxy send `X-Forwarded-Proto` and set `FORWARDED_ALLOW_IPS` to
the proxy's address in the container's environment (add it under
`environment:` in `docker-compose.yml`; uvicorn trusts forwarded headers only
from `127.0.0.1` by default), or terminate TLS at the app with [`HTTPS=1`](#https-and-installing-the-app-on-your-phone). Every request except the page itself, its
static files, `/api/auth` and `/cert.pem`/`/cert.crt` needs the cookie, including `/files/` links, so
scripts and the [link-sending shortcuts](#sending-links-from-your-phone-or-browser)
work only from a browser that has unlocked. A wrong PIN is answered after a
1-second delay, and after 5 wrong PINs in a minute from one address further
attempts are refused for the rest of that minute.

The PIN keeps housemates and guests on your network out; it is not a
substitute for network security. It travels in plain text over HTTP, and a
short numeric PIN can still be guessed by someone patient. Keep the app on a
trusted LAN (or behind a VPN, or HTTPS with real authentication) regardless.
With `ACCESS_PIN` empty nothing changes.

## HTTPS and installing the app on your phone

Phones only offer the share sheet, notifications and installing the page as
an app for pages served over HTTPS, and a LAN address over plain HTTP does not
count. The app can serve HTTPS itself with a certificate it generates, with no
domain or outside service needed. Set both in `.env` and run
`docker compose up -d`:

```
HTTPS=1
TLS_HOSTS=192.168.1.10
```

`TLS_HOSTS` must list the address you type into the browser (the IP, a name
such as `nas.local`, or several separated by commas). The port stays the
same; open `https://192.168.1.10:8080`. The port then speaks only HTTPS, so
existing bookmarks, the bookmarklet and phone shortcuts from
[Sending links](#sending-links-from-your-phone-or-browser) must switch to
`https://`. When you open the page by an address
the certificate does not cover, the footer says so: add it to `TLS_HOSTS` and
restart.

The first visit shows a certificate warning, because the certificate is signed
by the app's own certificate authority, which your phone does not know yet.
Accept the warning once, then tap **Install certificate** in the footer (or
open `/cert.pem`) and install it so the warning goes away for good:

- **Android:** download the certificate, then open Settings → Security →
  Encryption & credentials → Install a certificate → CA certificate, and pick
  the downloaded `sns-downloader-ca.crt`. (The exact menu names vary a little
  between phone makers.)
- **iOS:** open `/cert.pem` in Safari and allow the download, then open
  Settings → Profile Downloaded → Install. After that go to Settings →
  General → About → Certificate Trust Settings and turn on full trust for
  **SNS Downloader CA**.

With HTTPS on, the [access PIN](#access-pin) cookie is marked `Secure` and the
PIN no longer travels in plain text.

The certificate authority you install is valid for 10 years. The server
certificate it signs is short-lived and renews itself: it is checked every
time the container starts and replaced when it is within 30 days of expiring
(or when `TLS_HOSTS` changes), with nothing to reinstall on the phone. A
container that is never restarted for over two years would serve an expired
one; the automatic restart after a yt-dlp update normally takes care of that.
Everything lives in `data/config/tls`; delete that directory and restart to
start over with a new certificate authority, which every phone must then
install again.

### Add to Home screen

Once the page is served over HTTPS with the certificate installed, it can be
installed as an app:

- **Android:** in Chrome open the menu (three dots) → **Install app** (older
  versions say **Add to Home screen**). The installed app opens full screen and
  also appears in the share sheet: share a video from YouTube, Instagram, X or
  any other app to **SNS DL** and the link goes straight into the box and
  **Check** runs. Apps that share the link inside a longer text are fine; the
  first `http(s)://` link in it is used.
- **iOS:** in Safari tap Share → **Add to Home Screen**. iOS does not let web
  apps appear in the share sheet, so keep using the
  [iOS Shortcuts recipe](#sending-links-from-your-phone-or-browser) to send
  links.

The app's service worker only caches the page itself, so it still opens when
the server is briefly unreachable; checks, downloads and files always go to
the network.

### Notifications

The bell in the footer (**Notify when done**) asks the browser once for
permission to show notifications; after that you get one when a download
finishes or fails while the tab is in the background (nothing pops up while
you are looking at the page). Click it again to turn it off. Desktop browsers
allow this over plain HTTP when the page is opened as `localhost` on the same
machine; opened by a LAN address they need HTTPS, and so do phones. Android
Chrome then notifies from an ordinary tab; an iPhone also needs the page added
to the Home Screen and opened from there. The choice is remembered per browser, so turn it on in each
one you use. If the browser has blocked notifications for the page, the bell
is greyed out: allow them in the browser's site settings first.

## Dependency versions and rebuilding

The app also updates yt-dlp by itself every night (see [Keeping yt-dlp up to
date](#keeping-yt-dlp-up-to-date)). If downloads start failing across the
board anyway, move to the newest image, which is rebuilt with the latest yt-dlp on
every push to `main` and once a week (Monday 04:00 UTC) regardless:

```bash
make pull      # published image: docker compose pull && docker compose up -d
make update    # built from source: rebuild without cache and restart
```

yt-dlp is left open-ended (`yt-dlp>=...`) so every rebuild takes the newest
release. The other dependencies in `requirements.txt` and
`requirements-dev.txt` are pinned below the next major version of what the
suite was tested on (for example `fastapi>=0.162,<1`). A rebuild can still
pick up a minor release, and 0.x packages such as FastAPI do change behaviour
in minors, so run the suite after a rebuild. To move to a new major version, edit the range, rebuild
(`make build`, and `uv pip install -p .venv/bin/python -r requirements-dev.txt`
for the venv), and run the full suite before deploying.

## Development

```bash
make venv     # create .venv and install deps
make test     # run the test suite (no network needed)
make check    # ruff, JS syntax check, unit tests (no browser tests): run before committing
node --check app/static/app.js && node --check app/static/sw.js   # JS syntax check
DATA_DIR=$PWD/data .venv/bin/uvicorn app.main:create_app --factory --reload --port 8080
```

`make check` runs `ruff check app tests tools` (config in `pyproject.toml`:
pycodestyle errors/warnings and pyflakes, 120-column lines), `node --check` on
`app/static/app.js` and `app/static/sw.js`, and the unit tests without
`tests/browser`. It needs `node` on `PATH` (any current Node.js; only the
syntax check uses it). Run `make browser-test` for the Playwright tests.
GitHub Actions (`.github/workflows/ci.yml`) runs the same checks and the browser tests on
every push and pull request, then builds the image for amd64 and arm64 and, on `main` and
`v*` tags, publishes it as `ghcr.io/jonchaka/snsdl`. A weekly schedule (Monday 04:00 UTC)
and the **Run workflow** button rebuild `latest` without the layer cache, so the image
picks up the newest yt-dlp and base-image fixes between pushes. GitHub pauses scheduled
workflows after 60 days without a commit; a push or a manual run resumes them.

The footer starts with the build identity, `snsdl <version> (<commit>)`: CI sets `APP_VERSION`
to the tag (or `latest` on `main`) and `APP_COMMIT` to the commit; `make up` fills them from the
local checkout; a plain `docker build` shows `snsdl dev`. Quote that text in bug reports.

Releasing: move the notes under `## [Unreleased]` in `CHANGELOG.md` to a new `## [1.2.3] - YYYY-MM-DD`
heading, commit, then `git tag v1.2.3 && git push origin main v1.2.3`. CI publishes the `1.2.3`
and `1.2` image tags and creates the GitHub release from that section
(`tools/release_notes.py`); a tag without a CHANGELOG section fails the release job.

Dependencies are pinned to the major versions the suite was tested on (see
"Dependency versions and rebuilding"). To bump one, edit its range in `requirements.txt`
or `requirements-dev.txt`, reinstall the venv
(`uv pip install -p .venv/bin/python -r requirements-dev.txt`), rebuild the
image (`make build`, which uses
`docker-compose.build.yml`), and run the full suite.

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
most 2 MiB, JPEG, PNG, WebP, GIF or AVIF), stored in `data/thumbs` and deleted with
their row, by **Clear all** and by the nightly cleanup. They are skipped when
`PROXY` is a SOCKS proxy.

To download only part of a video, fill in **Start** and/or **End** under
**Clip** (`90`, `1:30` or `1:02:03`; an empty Start means the beginning, an
empty End the end of the video). The cut is made at the nearest keyframe
without re-encoding, so a clip may start a few seconds before the time you
entered. Tick **Exact cut (slower, frame-accurate)** to cut at exactly the
times you entered instead: the video is re-encoded around the cut points,
which takes longer and costs some CPU (the row's range is marked `exact`).
When the video has chapters, a **Chapter…** dropdown fills Start and End
with the chosen chapter. Clipping works for MP4, MP3 and original audio; the row shows the
range and "Clipping…" while it runs, since ffmpeg reports no percentage. The
Clip row is hidden when the site does not report the video's length, for
playlists and when several links are pasted.

If the site offers subtitles, a **Subtitles** dropdown appears for MP4
downloads listing the site's own tracks and, where available, the automatic
captions in the video's own language (marked "auto"). The chosen track is
embedded in the MP4 as a soft subtitle stream you can switch on in your player.
**More languages** adds up to four further tracks of the same kind (site
tracks with a site track, automatic captions with an automatic one; one
download cannot mix the two): each is embedded as its own stream, or with
**Separate .srt file** written as its own `<name>.<lang>.srt`. The API takes
them as a comma-separated list such as `"en,de"` or `"auto:en,auto:fr"`.
For a playlist the choice applies to every video; a video without that
language simply downloads without subtitles. If the site refuses the subtitle
file itself (YouTube occasionally answers caption requests with `HTTP Error
429`), the video is downloaded again straight away without subtitles and the
job's `note` says "Subtitles could not be fetched (site refused); downloaded
without them"; **Retry** is not needed.

**Subtitle files.** Next to the Subtitles dropdown, **Embed** (the default)
or **Separate .srt file** chooses where the track goes. With the file option
the track is converted to SubRip and saved beside the video as
`<video name>.<lang>.srt`; the finished row gets a small `.srt` link next to
Download, zips include the file (`Title.en.srt`), and Keep, Remove, Clear all
and the nightly cleanup treat it together with its video. Like embedding, it
is offered for MP4 only (also for playlists); a video without that language
just has no subtitle file.

**SponsorBlock.** For YouTube links a **Remove sponsor segments** checkbox
cuts the parts that [SponsorBlock](https://sponsor.ajay.app/) users marked as
sponsor, self-promotion, interaction reminders, intro or outro. The cuts are
made at keyframes without re-encoding. It applies to YouTube only (other
sites have no SponsorBlock data), to single videos only (not playlists,
several links or subscriptions), and cannot be combined with a clip (the
checkbox is disabled while Start or End is set). A video nobody has marked
downloads unchanged. Rows with it show `no ads`. `SPONSORBLOCK=1` makes it the
page's default. If the SponsorBlock service cannot be reached, the video is
downloaded uncut instead of failing, with the `note` "Sponsor segments could
not be fetched; downloaded uncut" (a job that lost both extras carries both
notes, joined by `; `).

**H.264.** **Prefer H.264 (plays on older devices)**, in the quality row for
MP4, picks H.264 video with AAC audio at the chosen resolution instead of the
default AV1/VP9 preference, for TVs, older phones and editors that cannot
play newer codecs. It only prefers: nothing is re-encoded, so where the site
has no H.264 stream at that resolution you get the best one it has (YouTube
usually offers H.264 up to 1080p only). Rows with it show `h264`; it also
applies to playlists and several links. `PREFER_H264=1` makes
it the page's default.

**Split by chapters.** When a video has at least two chapters, **One file per
chapter (N chapters)** downloads it once and splits it into one file per
chapter (each carrying the embedded thumbnail and subtitles), shown as a
playlist group under the video's title with the usual **Download all**, zip
and **Remove finished**. Parts are named after their chapter (`Part N` when a
chapter has no title). It cannot be combined with a clip or with a separate
subtitle file, and it is not offered for playlists or several links. If
yt-dlp finds no chapters after all, the video is kept whole with the note
"No chapters found; downloaded whole". The webhook fires once per part.

**File name.** The **File name** field (single videos only) sets the name of
the downloaded file instead of the video's title. It is made safe first:
ASCII letters, digits, `-`, `_` and `.` are kept, accented letters lose
their accents, spaces and other characters become `_` (`My video: part 1`
gives `My_video_-_part_1`), leading dots and dashes are removed, and it is cut to 150 characters (422 `file name has no usable
characters` when nothing is left). The extension is added for you. Split
parts keep the chapter title after your name.

Each row has a **Remove** button that deletes that file immediately instead of
waiting for the nightly cleanup. On a queued or downloading job the button
reads **Cancel**: it stops the download and discards any partial file.

## Live streams

When Check finds a stream that is live right now, the options show a `LIVE`
badge instead of the length, and **Download** records it with ffmpeg from
that moment on (not from the start of the broadcast: going back in time is
not supported) until you press **Stop** on the row, the stream ends, or
`LIVE_MAX_HOURS` (default 12) is reached. The row reads `Recording · 1:02:03 ·
1.2 GB` while it runs; when it stops the file so far is kept and the row
says why (`Stream ended`, `Recording stopped`, `Recording stopped after 12 h
(LIVE_MAX_HOURS)`, `Recording stopped: low disk space`). **Cancel** discards
the recording instead. A stream that has not started yet is refused at Check
("check again when it is live"), and one that ended before its recording
began is downloaded like an ordinary video.

- MP4 keeps the stream's own video and audio (no re-encoding; the best stream
  at or below the height you pick, or the best available one when none is that
  small). **Audio (MP3)** re-encodes the sound to MP3;
  **Audio (original)** copies the audio into an `.m4a`, which only works for
  streams whose audio is AAC or MP3 (most are); pick MP3 otherwise.
- Clips, SponsorBlock, subtitles and splitting do not apply to a live
  recording. A recording starts at once, ignoring the [download
  window](#download-window), and does not take a download slot; at most 3 run
  at the same time (more wait in the queue).
- `PROXY` must be an `http`/`https` proxy or empty: ffmpeg cannot use a SOCKS
  proxy, so a recording under one fails with that message. `RATE_LIMIT` does
  not apply to recordings.
- Stopping the container ends running recordings gracefully (the files are
  kept, with the note `Recording stopped: app restarting`). A hard kill
  (`docker kill`, a power cut) loses the recording in progress: the row comes
  back as failed (`interrupted by restart`) and the partial file is deleted.
- The nightly yt-dlp update restarts the app only when nothing is
  downloading, so a running recording delays that restart until it ends;
  queued downloads keep starting meanwhile.
- The stream's HTTP headers, including a Cookie header when a cookies file
  applies to that site, are passed to ffmpeg on its command line, so another
  user on the same host could read them from the process list while a
  recording runs (the job log never shows them). Prefer a use-once cookies
  file for live recordings.

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

## Subscriptions

**Subscriptions** (above the download list) follows a YouTube channel,
playlist or any other list yt-dlp can read: paste its link, choose format,
quality and **How many per check** (1–100, default 10) and press **Add**.
At `SUBSCRIPTION_CRON` (default every 6 hours; empty = manual only) and when
you press **Check now**, the app lists the channel and queues the entries it
has not seen before as ordinary rows, grouped under the subscription's title
like a playlist. The first check only remembers what is already there, so
only videos published afterwards are downloaded; tick **also download what
is already there** to queue the current entries too (up to the per-check
number). Entries are taken in the order the site lists them (newest first
for channels); when a check finds more new entries than the per-check
number, only that many are queued and the rest are marked as seen too: they
are skipped, not saved for the next check (see catching up below). An entry already queued with the
same settings is not added twice. Each row shows when it was last checked and
how many it queued, or the error of the last check (a site error or low disk
space; nothing is queued then). **Pause** stops the scheduled checks for one
subscription (Check now still works), **Remove** deletes the subscription but
leaves its downloads. Subscriptions are stored in `data/subscriptions.json`.
Each subscription remembers at most the 5000 most recent entries it has seen;
on a very large channel, older entries beyond that can come back as new if
the site lists them again.

Tick **catch up on older entries over later checks** to keep what one check
could not take: the new entries beyond the per-check number then stay unseen,
and each following check queues the next ones in the order the site lists
them (newer entries published meanwhile come first), until none are left.
The row shows `· 37 more to catch up` while some are waiting and
`· catching up` while the option is on. Together with **also download what
is already there** this downloads a whole channel a few entries per check;
without it, catching up only covers what is published after you subscribe
(the first check still just remembers what is already there).

### Podcast feed

Every subscription has a podcast feed (RSS with iTunes tags) of its finished
downloads, newest first, with title, description, length and cover image.
**Feed** on the subscription's row copies its address, which looks like
`http://192.168.1.10:8080/feed/<id>/<key>.xml`; paste it into a podcast app
(an MP3 or original-audio subscription makes a regular audio podcast; MP4
works in apps that play video). Episodes disappear from the feed when their
files are cleaned up, so use [retention](#retention) or **keep
automatically** for a feed you listen to later.

The 32-character key in the address is the credential: it is derived from
the server's secret (`data/config/secret`), works **without the access PIN**
(podcast apps cannot type one) and only opens that subscription's own feed,
files and thumbnails; a wrong key is a plain 404. Treat the address like a
password and do not share it beyond the devices that need it. The server's access log shows `***` in its place. Deleting
`data/config/secret` changes every key (and signs every browser out of the
PIN). The links inside the feed start with `PUBLIC_URL` when it is set,
otherwise with the address the podcast app used; set `PUBLIC_URL` when the
app reaches the server through another name or a reverse proxy.

## History

**History (N)**, between the subscriptions and the download list, lists what
was downloaded before, newest first, kept in `data/history.json` (at most
1000 entries): title, when, format and size. It survives the nightly cleanup,
retention and **Clear all**; a filter box searches title, link, site and
uploader. **Download again** queues the same link with the same options;
an entry's remove button forgets it and **Clear history** all of them. A
download repeated with the same options replaces its older entry, and a video
split into chapters counts once. After **Check**, a link downloaded before
shows `Downloaded before · 3 days ago · MP4 1080p` under its title.

## Playing a download in the page

Finished videos and audio files have a **Play** button next to Download: it
opens a player in the page that streams the file from the server (seeking
works; the browser fetches only the parts it needs). Photo posts show their
pictures in a scrollable column. Closing the player (Close, Escape or a click
beside it) stops playback. The player uses `/files/<name>?inline=1`, which
serves the file with its media type instead of as a download; files a browser
cannot play (for example `.mkv`) are still downloaded. Subtitles are not shown
in the page player: use the downloaded file in a desktop or TV player for
those. What plays depends on the browser (HEVC or AV1 video may not play
everywhere; **Prefer H.264** gives the safest MP4).

## Backup and restore

**Backup** in the footer downloads `sns-backup-YYYYMMDD-HHMM.zip` with your
subscriptions (`subscriptions.json`), the download history (`history.json`),
the queue pause (`state.json`) and the cookie files (`config/cookies.txt` and
the cookies added in the page, `config/cookies/*.txt`), plus a
`manifest.json`. Downloads, the job list, thumbnails, the server certificate
and the PIN secret are not included. **The backup contains your login
cookies: keep it private** like a password. Without `ACCESS_PIN` anyone on
your network can download it, cookies included.

**Restore…** picks such a zip and, after a confirmation, **replaces**
subscriptions, history, the queue pause and the cookies added in the page with
the backup's (cookies deleted since are not merged back; the hand-managed
`config/cookies.txt` is replaced only when the backup has one). Everything in
the file is checked first (only the files above, each at most 5 MB, at most
200 entries including the manifest, 20 MB in all both as a zip and unpacked,
valid JSON and cookie files); if anything is wrong
nothing is changed and the page shows why. A restore is refused while a
subscription check runs; try again when it ends.

## Download window

`DOWNLOAD_WINDOW=01:00-07:00` lets queued downloads start only between those
times, in `TZ` (a window may wrap past midnight: `22:00-06:00`). Outside it
jobs still queue as usual but wait; a banner above the list says when the
next start is, the footer shows the window, and each waiting row reads
`Waits for 01:00` with a **Download now** button that starts that one
straight away (the row then shows `now`). A download that is running when
the window closes finishes. Pause and Retry drop a row's "now" (a row that
keeps it also skips the window after a restart; other queued jobs wait for it
again). Empty (the default)
means downloads may start at any time.

## Webhook

With `WEBHOOK_URL` set, the server POSTs a JSON message there each time a
download finishes or fails for good (not before an automatic retry):

```json
{"event": "done", "job": {"id": "...", "url": "...", "title": "...", "status": "done", "filename": "...", ...},
 "file_url": "https://192.168.1.10:8080/files/abc-Title.mp4", "sent_at": "2026-11-06T03:12:00+00:00"}
```

`event` is `done` or `failed`; `job` is the job as `GET /api/jobs` returns
it; `file_url` is `PUBLIC_URL` + `/files/<name>` when `PUBLIC_URL` is set, the
relative `/files/<name>` otherwise, and `null` without a file. Headers:
`Content-Type: application/json`, `X-SNS-Event: done|failed` and, with
`WEBHOOK_SECRET` set, `X-SNS-Signature: sha256=<hex>`, the HMAC-SHA256 of the
raw request body keyed with the secret (compute it over the bytes you
received and compare in constant time). Any 2xx answer counts as delivered;
anything else (an error, a timeout after 5 seconds, a redirect, which is not
followed) is retried once 30 seconds later and then dropped; a retry still
waiting when the container restarts is lost. A message whose retry also
failed is saved in `data/config/webhook-pending.json` (at most 100) and sent
once more 30 seconds after the next start, then dropped whatever happens.
Failures are logged with the host only. The URL is configured on the server only; the
page cannot change it.

## Telegram

With `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set, every finished download
is also sent to a Telegram chat, and every failed one gets a short message.

1. In Telegram, open a chat with **@BotFather**, send `/newbot` and follow the
   questions. It answers with the bot's token (`123456789:AA...`): that is
   `TELEGRAM_BOT_TOKEN`.
2. Open a chat with your new bot and send it any message (a bot cannot write
   to you first). For a group, add the bot to the group and post a message
   there; for a channel, make the bot an administrator.
3. Open `https://api.telegram.org/bot<token>/getUpdates` in a browser: the
   `"chat": {"id": ...}` of your message is `TELEGRAM_CHAT_ID` (groups and
   channels have negative ids such as `-1001234567890`). A chat-id bot such as
   @userinfobot tells you your own id too.
4. Put both in `.env`, run `docker compose up -d`, and press **Send test
   message** in the page footer: the chat should get `SNS Downloader is
   connected.` The footer says `Telegram failed (see server log)` otherwise.

What is sent:

- **Videos and audio** go as the file itself, with the title and duration as
  the caption, when the file is at most 50 MB (the Bot API upload limit).
  Larger files are sent as a message with a link to the file instead; the link
  starts with `PUBLIC_URL` when it is set, otherwise it is a relative
  `/files/...` path and the message says to set `PUBLIC_URL` for a clickable
  link.
- **Photo posts** go as an album of up to ten photos (each at most 10 MB, the
  caption on the first). When the post has more files, or files Telegram does
  not take as photos, a second message gives a link to the whole post as a zip.
  A post with no suitable photo is sent as that link only.
- **Failed downloads** send `Failed: <title> · <reason>`. A job waiting for an
  [automatic retry](#per-site-pacing-and-automatic-retry) sends nothing yet.
- A split video sends one message per part, in order. Deliveries go out one at
  a time.

A delivery that fails is tried once more 30 seconds later, picking up after
what already went through (an album is not sent twice), and is then dropped;
nothing is kept across a restart. A download whose file was deleted before it
could be sent sends nothing. The token is part of every Bot API address, so the
server never logs those addresses; failures are logged with the method and the
error only, with the token blanked out of anything Telegram echoes back.

## Several links at once

Paste several links into the box (one per line or separated by spaces;
Shift+Enter adds a new line, Enter submits) and press **Check**. The page skips
the per-video check and offers the format and a generic quality list ("Best
available", "Up to 1080p"…); **Download** queues one ordinary row per link.
Duplicate links are queued once, links already queued with the same format,
quality and H.264 preference are reused, and anything that is not an `http://` or `https://` link is
listed as skipped. `MAX_PLAYLIST_ITEMS` caps how many links one paste can
queue. Pasting into the empty box runs **Check** straight away.

## Photo posts

A link to a post of photos (an Instagram carousel, an X/Twitter post with
images, a Pinterest pin, an Imgur album…) is handled by
[gallery-dl](https://github.com/mikf/gallery-dl) when yt-dlp finds no video
in it. **Check** then shows the title and `6 photos` (or `6 photos · 1 video`
for a mixed post) instead of the video options, and **Download** saves every
file of the post: the row offers **Download zip** (`stem-01.jpg`,
`stem-02.jpg`, …) when there is more than one file, otherwise the file
itself. Only these sites are accepted (the `images.sites` list of
`/api/status`): Instagram, X/Twitter, Facebook, Pinterest, Tumblr, Reddit,
Bluesky, Imgur, TikTok, Flickr, VSCO and Weibo; other links answer `This site
is not supported for photo posts`. Threads is not supported. Instagram and X
show photo posts only to a logged-in session, so add your
[cookies](#instagram--facebook-private-or-login-walled-posts) for them (the
failed row offers **Add cookies**). At most 200 files are taken from one post,
and one gallery-dl operation (a check or a download) runs at a time; others
wait their turn. Links pasted several at once or shared from the phone skip
**Check**: they are queued as videos and switch to photos by themselves when
yt-dlp finds no video. Photo posts are recorded in [history](#history) like
any download but left out of podcast feeds.

## Keeping yt-dlp up to date

Sites change often and the fix is almost always a newer yt-dlp. Every night at
`UPDATE_CRON` (04:00 by default, after the 03:00 cleanup) the app runs
`pip install --upgrade yt-dlp` into `data/lib`, which is on the container's
`PYTHONPATH`, so the newer copy shadows the one baked into the image and
survives `docker compose up --build`. If the installed version differs from
the running one, the app first waits for running live recordings to end
(downloads keep running and starting meanwhile), then stops starting new
downloads, waits for running ones to finish, and exits; Docker's `restart: unless-stopped` policy brings it back
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

The page gets live updates from `GET /api/events` (Server-Sent Events) and
falls back to polling `/api/jobs` and `/api/status` every 2 seconds when the
stream drops; scripts can use either.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/auth` | `{"required": bool, "ok": bool}`: whether `ACCESS_PIN` is set and whether this browser's cookie passes (`{"required": false, "ok": true}` without a PIN). Never 401. |
| `POST` | `/api/auth` | `{"pin": "..."}` → 204 with the `sns_auth` cookie (`HttpOnly; SameSite=Lax; Path=/; Max-Age=31536000`, plus `Secure` over HTTPS). A wrong PIN answers 401 after 1 second; 429 `{"detail": "Too many attempts, wait a minute"}` once an address has 5 wrong PINs within 60 seconds. Without a PIN configured: 204 and no cookie. Needs header `X-Requested-With: sns-downloader`. |
| `DELETE` | `/api/auth` | Clear the cookie (204), i.e. lock this browser. Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/api/cookies` | `{"entries": [{"id", "source": "kept"\|"legacy", "sites", "count", "added_at", "expires_at", "expired"}]}` — metadata only, never cookie values. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/cookies` | `{"text": "<Netscape cookies.txt>", "keep": bool, "job_id": optional}` → `{"entry", "retried"}` (201). `keep=false` needs `job_id` and is used by that job only; a failed `job_id` is re-queued. 422 with a reason if the text is not a cookies file; 404 unknown job. Needs the header. |
| `DELETE` | `/api/cookies/{id}` | Delete a kept entry (204; 404 unknown or legacy). Needs the header. |
| `POST` | `/api/probe` | `{"url": "..."}` → `{"kind": "video", "title", "duration", "heights": [2160, 1080, ...], "sizes": [{"height", "width", "bytes"}], "audio_bytes", "subtitles": [{"lang", "name", "auto"}], "thumb", "chapters": [{"title", "start", "end"}], "playlist": null \| {"title", "count", "total"}}`. Lists the video resolutions available. A [photo post](#photo-posts) answers `{"kind": "images", "title", "count", "video_count", "files": [{"ext", "width", "height"}], "thumb", "previous"}` instead (`thumb` is the first picture, fetched by the server). `duration` is whole seconds (`null` for live streams or when unknown); `sizes[].bytes` is the estimated file size at that height including audio, and `audio_bytes` that of the best audio-only stream (`null` when the site does not say); `thumb` is a thumbnail key for `/api/thumbs/{key}` or `null`; `chapters` are the video's chapters in seconds (at most 200, `[]` when it has none); `live` is true for a stream that is live now (422 when it has not started yet); `previous` is `{"finished_at", "format", "quality", "title"}` of the newest [history](#history) entry for that link, else `null`. 422 `{"detail", "needs_login"}` if the site refuses (`needs_login` true when it wants a logged-in session). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs` | `{"url": "...", "format": "mp4"\|"mp3"\|"audio"\|"images", "quality": "..."}` → job (201 new, 200 if the same url, format, quality, subtitles, subtitle mode, clip, `sponsorblock` and `compat` is already active, 422 invalid). Optional `"sponsorblock": true` cuts SponsorBlock segments on YouTube (stored but without effect on other sites; 422 `sponsor removal cannot be combined with a clip`); optional `"compat": true` prefers H.264/AAC (mp4 only, 422 otherwise); optional `"subtitles_mode": "embed"` (default) or `"file"` (a separate `.srt`; needs `subtitles`, 422 otherwise). `format` defaults to `mp4`, `quality` to `best`. For `mp4`, `quality` is a height such as `1080`; for `mp3` it is `best`, `320` or `192`; for `audio` (best stream, no re-encode) it must be `best`; `images` (a [photo post](#photo-posts), quality `best`) refuses subtitles, clips, `sponsorblock`, `compat`, `split` and `live` (422 `photos have no subtitles`, `photos cannot be clipped`, `photos have no sponsor data`, `photos have no codec preference`, `photos cannot be split`, `photos are not live`) but takes `filename`; `/api/jobs/batch` and `/api/playlists` refuse it (422 `photos are single posts`). Optional `"subtitles": "en"` or `"auto:en"` (mp4 only; a `lang` from the probe) embeds that track; a comma-separated list of up to 5 (`"en,de"`, `"auto:en,auto:fr"`) fetches each, all of one kind (422 `subtitles: up to 5 languages, comma-separated`, also for an empty entry such as `"en,,de"` or `"en,"`, and `choose either site subtitles or automatic captions, not both`; duplicates are dropped). Optional `"clip_start"` / `"clip_end"` (seconds, 0–9999999, end after start; either may be left out) download only that range; different clips of one video are separate jobs. Optional `"clip_exact": true` cuts frame-accurately by re-encoding at the cut points (422 without a clip; not accepted by `/api/playlists`). Optional `"thumb"`: the key `/api/probe` returned (422 if unknown). Optional `"live": true` records a live stream (starts at once; 422 `a live stream cannot be clipped`, `... has no sponsor data`, `... has no subtitles`, `... cannot be split`). Optional `"split": true` makes one file per chapter (422 `chapters cannot be split from a clip`, `a separate subtitle file cannot be combined with splitting`). Optional `"filename"` (at most 150 characters) names the file (422 `file name has no usable characters`); `split` and the file name are part of the duplicate check. `/api/playlists` refuses `live`, `split` and `filename` (422). 507 `{"detail": "Only 600.0 MB free on the download disk; downloads need at least 1.0 GB"}` when free space is below `MIN_FREE_SPACE`; the same 507 applies to batch, playlists, both retry routes and cookies that would re-queue a failed job. |
| `POST` | `/api/jobs/batch` | `{"urls": ["...", ...], "format": "...", "quality": "...", "compat": optional}` (1 to 2000 entries; same format, quality and `compat` rules as `/api/jobs`; `sponsorblock` is refused with 422) → `{"queued", "reused", "rejected": ["..."], "jobs": [...]}` (201). Links are deduplicated in order, invalid ones skipped and the first 20 listed in `rejected` (each cut to 200 characters), active jobs with the same url, format, quality and `compat` reused. 422 if no link is valid or the distinct valid links exceed `MAX_PLAYLIST_ITEMS`; 507 low disk. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/playlists` | `{"url": "...", "format": "...", "quality": "...", "limit": N, "subtitles": optional, "subtitles_mode": optional, "compat": optional}` (`sponsorblock` is refused with 422: single videos only) → `{"playlist", "total", "queued", "reused", "jobs": [...]}` (201). Queues one job per playlist entry, at most `limit` and `MAX_PLAYLIST_ITEMS`; entries already active with the same format, quality, subtitles, `subtitles_mode` and `compat` are reused. 422 with a message if the URL is not a playlist, the site refuses, or the body is invalid (clips apply to single videos only); 507 low disk; 403 without the header. |
| `DELETE` | `/api/playlists/{playlist_id}?status=active\|finished` | `status=active` (default): cancel every queued, paused or downloading job created by that playlist request → `{"cancelled": n}`; finished rows stay. `status=finished`: remove every done or failed row of that playlist (kept rows excepted) and delete its file → `{"removed": n}`. 404 unknown id, 422 other status values. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/retry` | Put one failed job back in the queue (same id and row) → the job (404 unknown, 409 if not failed). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/playlists/{playlist_id}/retry` | Re-queue every failed job of that playlist request → `{"retried": n}` (404 unknown id). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/keep` | Exempt a finished download from the nightly cleanup and Clear all, moving its file into `KEEP_DIR` when set → the job (404 unknown, 409 if not done; 507 when the move runs out of disk space, 500 `{"detail": "Could not move the file: ..."}` for any other move error, with `keep` unchanged). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/unkeep` | Undo keep (moving the file back from `KEEP_DIR`) → the job (404 unknown, 409 if not done; 507/500 like keep when the move fails). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/pause` | Hold a queued job back (status `paused`) → the job (404 unknown, 409 if not queued). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/next` | Download a queued job next: moves it to the front of the queue and sets `priority` → the job (404 unknown, 409 if not queued). Pausing the job clears `priority`. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/{id}/now` | "Download now": start a queued job even while the [download window](#download-window) is closed; sets `now` and moves the job to the front of the queue → the job (404 unknown, 409 if not queued). Pause and retry clear `now`. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/search` | `{"q": "..."}` (at most 400 characters as sent, 1 to 200 after trimming) → `{"results": [{"url", "title", "duration", "uploader", "thumb"}]}`: the first 8 YouTube results; `duration` in whole seconds or `null`, `thumb` a key for `/api/thumbs/{key}` or `null`. 422 for an empty or too long query, or `{"detail"}` when the search fails. Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/api/subscriptions` | `{"subscriptions": [{"id", "url", "title", "format", "quality", "subtitles", "limit", "enabled", "include_existing", "auto_keep", "added_at", "last_checked", "last_error", "last_queued", "seeded", "backfill", "last_remaining", "seen_count", "feed"}]}`, oldest first. `title` is `null` until the first good check; `feed` is the relative path of its [podcast feed](#podcast-feed) (`/feed/<id>/<key>.xml`; the key is a credential); `last_remaining` is how many new entries the last check left for later (always 0 without `backfill`) |
| `POST` | `/api/subscriptions` | `{"url", "format", "quality", "subtitles": optional, "limit": 1–100 (default 10), "include_existing": false, "auto_keep": false, "backfill": false}` → the subscription (201); its first check starts in the background. 422 invalid (same format/quality/subtitles rules as `/api/jobs`; `keep automatically needs KEEP_DIR` for `auto_keep` without `KEEP_DIR`; `subscriptions download videos or audio` for `images`), 507 low disk. Needs header `X-Requested-With: sns-downloader`. |
| `DELETE` | `/api/subscriptions/{id}` | Remove a subscription; its jobs stay (204; 404 unknown). Needs the header. |
| `POST` | `/api/subscriptions/{id}/check` | Check now and queue what is new → `{"queued": n, "error": null\|"..."}` (404 unknown, 409 `{"detail": "A check is already running"}`, 507 low disk). Needs the header. |
| `POST` | `/api/subscriptions/{id}/pause`, `/api/subscriptions/{id}/resume` | Stop or restart the scheduled checks of one subscription → the subscription (404 unknown). Needs the header. |
| `POST` | `/api/jobs/{id}/stop` | Stop a running live recording and keep what was recorded → the job with `stop_requested: true` (404 unknown, 409 `Only a running live recording can be stopped`). Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/api/history?q=&limit=` | `{"entries": [{"id", "url", "title", "format", "quality", "subtitles", "subtitles_mode", "compat", "sponsorblock", "clip_start", "clip_end", "clip_exact", "split", "size", "duration", "thumb", "uploader", "site", "finished_at"}], "total": n}`, newest first; `q` (at most 200 characters) matches title, url, site or uploader; `limit` 1–1000 (default 200); `total` counts every entry |
| `DELETE` | `/api/history` | Forget every history entry (204); files and jobs are not touched. Needs the header. |
| `DELETE` | `/api/history/{id}` | Forget one history entry (204; 404 unknown). Needs the header. |
| `GET` | `/api/events` | Server-Sent Events (`text/event-stream`, not cached): `event: jobs` (the `/api/jobs` list) on connect and after every change, `event: status` (the `/api/status` body) on connect and every 5 seconds, and a `: keepalive` comment after 15 quiet seconds |
| `GET` | `/feed/{id}/{key}.xml` | The subscription's podcast feed (`application/rss+xml`, not cached). `/feed/{id}/{key}/{name}` serves one of its finished files and `/feed/{id}/{key}/thumbs/{thumb}` one of its thumbnails. 404 for a wrong key or anything that is not that subscription's. Never asks for the PIN: the key is the credential |
| `POST` | `/api/jobs/{id}/resume` | Put a paused job back in the queue, at the end → the job (404 unknown, 409 if not paused). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/playlists/{playlist_id}/pause` | Pause every queued job of that playlist request → `{"paused": n}` (404 unknown id). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/playlists/{playlist_id}/resume` | Resume every paused job of that playlist request → `{"resumed": n}` (404 unknown id). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/bulk` | `{"ids": ["...", ...], "action": "keep"\|"unkeep"\|"remove"}` (1 to 500 ids) → `{"done": n, "skipped": n}`. `keep`/`unkeep` apply to done jobs only (moving files to or from `KEEP_DIR`); `remove` acts like `DELETE /api/jobs/{id}` per id. Unknown, repeated and not-applicable ids, and files that could not be moved, count as skipped; 422 for no ids, more than 500 or another action. Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/api/zip?ids=a,b,c` | Stream a zip (stored) of the finished files among those job ids, named `title.ext` (duplicates numbered), each followed by its subtitle files named after the video's entry (`title.lang.srt`, `title (2).lang.srt`); 404 when none has finished, 422 for no ids or more than 500. Photo posts contribute their files as `stem-01.jpg`, `stem-02.jpg`… (`NNN-stem-01.jpg` in a playlist zip). |
| `GET` | `/api/jobs/{id}/qr.svg` | A QR code (`image/svg+xml`, not cached) of the file's `/files/` URL, built from the scheme and `Host` the request used; 404 unknown job or file gone, 409 if not done, 422 when the URL is over 1500 characters |
| `GET` | `/api/playlists/{playlist_id}/zip` | Stream a zip (stored, not compressed) of that playlist's finished files, named `NNN-title.ext` by playlist position (subtitle files `NNN-title.lang.srt`) (404 unknown id or nothing finished yet). Photo posts contribute their files as `stem-01.jpg`, `stem-02.jpg`… (`NNN-stem-01.jpg` in a playlist zip). |
| `GET` | `/api/jobs` | All jobs, newest first (failed jobs carry `needs_login` when cookies may help). Jobs carry `clip_start`, `clip_end`, `clip_exact`, `priority`, `thumb`, `attempts` and `retry_at` (ISO time of a pending automatic retry, else `null`; such a job is `queued` with `error` starting `retrying: `), `sponsorblock`, `compat`, `subtitles_mode` (`embed`/`file`), `sidecars` (names of the job's `.srt` files, or a photo post's further files, served by `/files/`), `subscription_id` (the subscription that queued it, else `null`), `now` ("Download now" pressed), `live`, `stop_requested`, `split`, `custom_name` (the sanitized file name), `subdir` (the media layout folder of a kept file, else `null`), `finished_at`, `duration`, `uploader`, `upload_date`, `description` and `note` (on a done job, why subtitles or sponsor cuts were dropped, else `null`); the failure log is not included |
| `GET` | `/api/jobs/{id}/log` | `{"log": "..."}`: the last yt-dlp (or gallery-dl for photo posts) lines of a failed download (at most 8 KB, temporary paths and cookie file names blanked), `""` when there is none (404 unknown) |
| `GET` | `/api/thumbs/{key}` | A stored thumbnail image, cached for a day (404 unknown) |
| `DELETE` | `/api/jobs/{id}` | Remove one job and its file (and subtitle files); on a queued or downloading job this cancels it (404 unknown). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/cleanup` | Delete all files and clear the list, except kept rows (waiting and running jobs are cleared too, unlike the nightly run). Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/api/status` | `{"yt_dlp_version", "downloads": {"files", "bytes"}, "queue_paused", "rate_limit", "impersonate": {"target", "active"}, "update": {...}, "disk": {"free", "min_free", "low"}, "proxy", "pacing": {"max_per_site", "site_gap"}, "keep_dir", "https", "cert_hosts", "window", "webhook", "subscriptions": {"count", "cron"}, "defaults": {"sponsorblock", "prefer_h264"}, "retention_days", "live_max_hours", "recordings": {"active", "max"}, "media_layout", "public_url", "history": {"count"}, "images": {"sites"}, "telegram"}` — shown in the page footer. `disk` is in bytes (`low` true below `MIN_FREE_SPACE`); `proxy` is the proxy URL without credentials, or `null`; `downloads` includes `KEEP_DIR`; `keep_dir` is true when one is set; `https` is true with `HTTPS` on, and `cert_hosts` lists the names and addresses the server certificate covers (`[]` when off); `window` is `null` without `DOWNLOAD_WINDOW`, else `{"from": "01:00", "to": "07:00", "active": bool, "next_start": ISO time in `TZ` or `null` while open}`; `webhook` is true when `WEBHOOK_URL` is set (the URL and secret are never returned); `subscriptions.cron` is `SUBSCRIPTION_CRON` or `null` when empty; `defaults` are the `SPONSORBLOCK` and `PREFER_H264` page defaults; `retention_days` is `RETENTION_DAYS` or `null`; `recordings` counts running live recordings against the limit of 3; `public_url` is `PUBLIC_URL` or `null`; `images.sites` lists the [photo post](#photo-posts) sites (gallery-dl categories); `telegram` is true when [Telegram](#telegram) is configured (the token and chat id are never returned) |
| `POST` | `/api/queue/pause` | Stop starting new downloads (running ones finish); persists across restarts → `{"queue_paused": true}`. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/queue/resume` | Start taking jobs again → `{"queue_paused": false}`. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/update` | Install the newest yt-dlp into `data/lib` now → `{"running", "installed", "restart_pending", "last_checked", "error", "note"}`; restarts when idle if the version changed. Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/cert.pem`, `/cert.crt` | The certificate authority to install on a phone (see [HTTPS](#https-and-installing-the-app-on-your-phone)), as the download `sns-downloader-ca.crt` (`application/x-x509-ca-cert`, not cached); 404 `{"detail": "HTTPS is off"}` without `HTTPS`, 404 `{"detail": "Certificate not found"}` when the file is missing. Never asks for the PIN |
| `GET` | `/files/{filename}` | Download a finished file or one of its `.srt` subtitle files (looked up in `data/downloads`, then `KEEP_DIR`); `/files/{folder}/{filename}` for a file kept in a [media server layout](#media-server-layout) folder |
| `GET` | `/files/{filename}?inline=1` | The same file for the [page player](#playing-a-download-in-the-page): `Content-Disposition: inline` and the media type from the extension (`mp4`, `webm`, `mov`, `m4v`, `m4a`, `mp3`, `opus`, `ogg`, `oga`, `flac`, `wav`, `jpg`/`jpeg`, `png`, `webp`, `gif`, `avif`); `Range` requests answer 206. Any other extension is still a download |
| `GET` | `/api/backup` | The [backup](#backup-and-restore) zip (`application/zip`, `Content-Disposition: attachment; filename="sns-backup-YYYYMMDD-HHMM.zip"`, not cached); 507 when the files exceed 20 MB. Contains login cookies |
| `POST` | `/api/telegram/test` | Send `SNS Downloader is connected.` to `TELEGRAM_CHAT_ID` → `{"ok": bool}` (`false` when Telegram refused it or could not be reached; the reason is in the server log). 404 when [Telegram](#telegram) is not configured. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/restore` | Multipart field `file` (or the zip as the raw body), at most 20 MB → `{"subscriptions": n, "history": n, "cookies": n}`; replaces those files and reloads them. 422 `{"detail": reason}` when anything does not validate (nothing is changed), 409 while a subscription check runs. Needs header `X-Requested-With: sns-downloader`. |

With `ACCESS_PIN` set, every route above except `/api/auth` and `/cert.pem`/`/cert.crt` (and the page and its static
files) answers 401 `{"detail": "PIN required"}` without a valid `sns_auth` cookie.

## License

SNS Downloader is free software under the
[GNU General Public License v3.0](LICENSE) (or, at your option, any later
version). It drives [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense),
[gallery-dl](https://github.com/mikf/gallery-dl) (GPLv2) and
[ffmpeg](https://ffmpeg.org/) as separate programs; the Docker image installs
them from their own distributions.

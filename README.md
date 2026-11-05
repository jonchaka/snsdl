# SNS Downloader

Self-hosted web app that downloads videos from TikTok, YouTube, Instagram,
Facebook and [any other site yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)
at the quality you choose (defaulting to 1080p or best available), without
watermarks, and hands you a download link. Everything is deleted automatically every night.

**Intended for a trusted LAN only.** There are no user accounts; an optional
[access PIN](#access-pin) keeps casual visitors out. Do not expose it to the
internet without putting HTTPS and real authentication in front of it.

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
| `PUBLIC_URL` | *(none)* | The address you open the app by, as an origin such as `https://192.168.1.10:8080` (no path); used for the absolute `file_url` in the webhook. Empty sends a relative `/files/...` path |
| `SUBSCRIPTION_CRON` | `0 */6 * * *` | When to check [subscriptions](#subscriptions) for new videos (cron syntax, in `TZ`); empty means manual checks only (**Check now**) |
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
      - ./data:/data
      - ./keep:/keep
```

then point `KEEP_DIR` at the mount in `.env` and run `docker compose up -d`:

```bash
KEEP_DIR=/keep
```

The directory must be writable by `PUID`. **Remove** deletes a kept file from
`KEEP_DIR` too. The footer's space total includes it.

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
are skipped, not saved for the next check. An entry already queued with the
same settings is not added twice. Each row shows when it was last checked and
how many it queued, or the error of the last check (a site error or low disk
space; nothing is queued then). **Pause** stops the scheduled checks for one
subscription (Check now still works), **Remove** deletes the subscription but
leaves its downloads. Subscriptions are stored in `data/subscriptions.json`.

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
followed) is retried once 30 seconds later and then dropped. Failures are
logged with the host only. The URL is configured on the server only; the
page cannot change it.

## Several links at once

Paste several links into the box (one per line or separated by spaces;
Shift+Enter adds a new line, Enter submits) and press **Check**. The page skips
the per-video check and offers the format and a generic quality list ("Best
available", "Up to 1080p"…); **Download** queues one ordinary row per link.
Duplicate links are queued once, links already queued with the same format,
quality and H.264 preference are reused, and anything that is not an `http://` or `https://` link is
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
| `GET` | `/api/auth` | `{"required": bool, "ok": bool}`: whether `ACCESS_PIN` is set and whether this browser's cookie passes (`{"required": false, "ok": true}` without a PIN). Never 401. |
| `POST` | `/api/auth` | `{"pin": "..."}` → 204 with the `sns_auth` cookie (`HttpOnly; SameSite=Lax; Path=/; Max-Age=31536000`, plus `Secure` over HTTPS). A wrong PIN answers 401 after 1 second; 429 `{"detail": "Too many attempts, wait a minute"}` once an address has 5 wrong PINs within 60 seconds. Without a PIN configured: 204 and no cookie. Needs header `X-Requested-With: sns-downloader`. |
| `DELETE` | `/api/auth` | Clear the cookie (204), i.e. lock this browser. Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/api/cookies` | `{"entries": [{"id", "source": "kept"\|"legacy", "sites", "count", "added_at", "expires_at", "expired"}]}` — metadata only, never cookie values. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/cookies` | `{"text": "<Netscape cookies.txt>", "keep": bool, "job_id": optional}` → `{"entry", "retried"}` (201). `keep=false` needs `job_id` and is used by that job only; a failed `job_id` is re-queued. 422 with a reason if the text is not a cookies file; 404 unknown job. Needs the header. |
| `DELETE` | `/api/cookies/{id}` | Delete a kept entry (204; 404 unknown or legacy). Needs the header. |
| `POST` | `/api/probe` | `{"url": "..."}` → `{"title", "duration", "heights": [2160, 1080, ...], "sizes": [{"height", "width", "bytes"}], "audio_bytes", "subtitles": [{"lang", "name", "auto"}], "thumb", "chapters": [{"title", "start", "end"}], "playlist": null \| {"title", "count", "total"}}`. Lists the video resolutions available. `duration` is whole seconds (`null` for live streams or when unknown); `sizes[].bytes` is the estimated file size at that height including audio, and `audio_bytes` that of the best audio-only stream (`null` when the site does not say); `thumb` is a thumbnail key for `/api/thumbs/{key}` or `null`; `chapters` are the video's chapters in seconds (at most 200, `[]` when it has none). 422 `{"detail", "needs_login"}` if the site refuses (`needs_login` true when it wants a logged-in session). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs` | `{"url": "...", "format": "mp4"\|"mp3"\|"audio", "quality": "..."}` → job (201 new, 200 if the same url, format, quality, subtitles, subtitle mode, clip, `sponsorblock` and `compat` is already active, 422 invalid). Optional `"sponsorblock": true` cuts SponsorBlock segments on YouTube (stored but without effect on other sites; 422 `sponsor removal cannot be combined with a clip`); optional `"compat": true` prefers H.264/AAC (mp4 only, 422 otherwise); optional `"subtitles_mode": "embed"` (default) or `"file"` (a separate `.srt`; needs `subtitles`, 422 otherwise). `format` defaults to `mp4`, `quality` to `best`. For `mp4`, `quality` is a height such as `1080`; for `mp3` it is `best`, `320` or `192`; for `audio` (best stream, no re-encode) it must be `best`. Optional `"subtitles": "en"` or `"auto:en"` (mp4 only; a `lang` from the probe) embeds that track. Optional `"clip_start"` / `"clip_end"` (seconds, 0–9999999, end after start; either may be left out) download only that range; different clips of one video are separate jobs. Optional `"clip_exact": true` cuts frame-accurately by re-encoding at the cut points (422 without a clip; not accepted by `/api/playlists`). Optional `"thumb"`: the key `/api/probe` returned (422 if unknown). 507 `{"detail": "Only 600.0 MB free on the download disk; downloads need at least 1.0 GB"}` when free space is below `MIN_FREE_SPACE`; the same 507 applies to batch, playlists, both retry routes and cookies that would re-queue a failed job. |
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
| `GET` | `/api/subscriptions` | `{"subscriptions": [{"id", "url", "title", "format", "quality", "subtitles", "limit", "enabled", "include_existing", "added_at", "last_checked", "last_error", "last_queued", "seeded", "seen_count"}]}`, oldest first. `title` is `null` until the first good check |
| `POST` | `/api/subscriptions` | `{"url", "format", "quality", "subtitles": optional, "limit": 1–100 (default 10), "include_existing": false}` → the subscription (201); its first check starts in the background. 422 invalid (same format/quality/subtitles rules as `/api/jobs`), 507 low disk. Needs header `X-Requested-With: sns-downloader`. |
| `DELETE` | `/api/subscriptions/{id}` | Remove a subscription; its jobs stay (204; 404 unknown). Needs the header. |
| `POST` | `/api/subscriptions/{id}/check` | Check now and queue what is new → `{"queued": n, "error": null\|"..."}` (404 unknown, 409 `{"detail": "A check is already running"}`, 507 low disk). Needs the header. |
| `POST` | `/api/subscriptions/{id}/pause`, `/api/subscriptions/{id}/resume` | Stop or restart the scheduled checks of one subscription → the subscription (404 unknown). Needs the header. |
| `POST` | `/api/jobs/{id}/resume` | Put a paused job back in the queue, at the end → the job (404 unknown, 409 if not paused). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/playlists/{playlist_id}/pause` | Pause every queued job of that playlist request → `{"paused": n}` (404 unknown id). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/playlists/{playlist_id}/resume` | Resume every paused job of that playlist request → `{"resumed": n}` (404 unknown id). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/jobs/bulk` | `{"ids": ["...", ...], "action": "keep"\|"unkeep"\|"remove"}` (1 to 500 ids) → `{"done": n, "skipped": n}`. `keep`/`unkeep` apply to done jobs only (moving files to or from `KEEP_DIR`); `remove` acts like `DELETE /api/jobs/{id}` per id. Unknown, repeated and not-applicable ids, and files that could not be moved, count as skipped; 422 for no ids, more than 500 or another action. Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/api/zip?ids=a,b,c` | Stream a zip (stored) of the finished files among those job ids, named `title.ext` (duplicates numbered), each followed by its subtitle files named after the video's entry (`title.lang.srt`, `title (2).lang.srt`); 404 when none has finished, 422 for no ids or more than 500 |
| `GET` | `/api/jobs/{id}/qr.svg` | A QR code (`image/svg+xml`, not cached) of the file's `/files/` URL, built from the scheme and `Host` the request used; 404 unknown job or file gone, 409 if not done, 422 when the URL is over 1500 characters |
| `GET` | `/api/playlists/{playlist_id}/zip` | Stream a zip (stored, not compressed) of that playlist's finished files, named `NNN-title.ext` by playlist position (subtitle files `NNN-title.lang.srt`) (404 unknown id or nothing finished yet) |
| `GET` | `/api/jobs` | All jobs, newest first (failed jobs carry `needs_login` when cookies may help). Jobs carry `clip_start`, `clip_end`, `clip_exact`, `priority`, `thumb`, `attempts` and `retry_at` (ISO time of a pending automatic retry, else `null`; such a job is `queued` with `error` starting `retrying: `), `sponsorblock`, `compat`, `subtitles_mode` (`embed`/`file`), `sidecars` (names of the job's `.srt` files, served by `/files/`), `subscription_id` (the subscription that queued it, else `null`), `now` ("Download now" pressed) and `note` (on a done job, why subtitles or sponsor cuts were dropped, else `null`); the failure log is not included |
| `GET` | `/api/jobs/{id}/log` | `{"log": "..."}`: the last yt-dlp lines of a failed download (at most 8 KB, temporary paths and cookie file names blanked), `""` when there is none (404 unknown) |
| `GET` | `/api/thumbs/{key}` | A stored thumbnail image, cached for a day (404 unknown) |
| `DELETE` | `/api/jobs/{id}` | Remove one job and its file (and subtitle files); on a queued or downloading job this cancels it (404 unknown). Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/cleanup` | Delete all files and clear the list, except kept rows (waiting and running jobs are cleared too, unlike the nightly run). Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/api/status` | `{"yt_dlp_version", "downloads": {"files", "bytes"}, "queue_paused", "rate_limit", "impersonate": {"target", "active"}, "update": {...}, "disk": {"free", "min_free", "low"}, "proxy", "pacing": {"max_per_site", "site_gap"}, "keep_dir", "https", "cert_hosts", "window", "webhook", "subscriptions": {"count", "cron"}, "defaults": {"sponsorblock", "prefer_h264"}}` — shown in the page footer. `disk` is in bytes (`low` true below `MIN_FREE_SPACE`); `proxy` is the proxy URL without credentials, or `null`; `downloads` includes `KEEP_DIR`; `keep_dir` is true when one is set; `https` is true with `HTTPS` on, and `cert_hosts` lists the names and addresses the server certificate covers (`[]` when off); `window` is `null` without `DOWNLOAD_WINDOW`, else `{"from": "01:00", "to": "07:00", "active": bool, "next_start": ISO time in `TZ` or `null` while open}`; `webhook` is true when `WEBHOOK_URL` is set (the URL and secret are never returned); `subscriptions.cron` is `SUBSCRIPTION_CRON` or `null` when empty; `defaults` are the `SPONSORBLOCK` and `PREFER_H264` page defaults |
| `POST` | `/api/queue/pause` | Stop starting new downloads (running ones finish); persists across restarts → `{"queue_paused": true}`. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/queue/resume` | Start taking jobs again → `{"queue_paused": false}`. Needs header `X-Requested-With: sns-downloader`. |
| `POST` | `/api/update` | Install the newest yt-dlp into `data/lib` now → `{"running", "installed", "restart_pending", "last_checked", "error", "note"}`; restarts when idle if the version changed. Needs header `X-Requested-With: sns-downloader`. |
| `GET` | `/cert.pem`, `/cert.crt` | The certificate authority to install on a phone (see [HTTPS](#https-and-installing-the-app-on-your-phone)), as the download `sns-downloader-ca.crt` (`application/x-x509-ca-cert`, not cached); 404 `{"detail": "HTTPS is off"}` without `HTTPS`, 404 `{"detail": "Certificate not found"}` when the file is missing. Never asks for the PIN |
| `GET` | `/files/{filename}` | Download a finished file or one of its `.srt` subtitle files (looked up in `data/downloads`, then `KEEP_DIR`) |

With `ACCESS_PIN` set, every route above except `/api/auth` and `/cert.pem`/`/cert.crt` (and the page and its static
files) answers 401 `{"detail": "PIN required"}` without a valid `sns_auth` cookie.

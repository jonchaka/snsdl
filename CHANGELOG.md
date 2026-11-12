# Changelog

Notable changes to snsdl. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-11-13

### Added
- First tagged release, published as `ghcr.io/jonchaka/snsdl` (amd64 and arm64).
- Downloads: paste a link, check it, pick Video (MP4), Audio (MP3) or Audio (original) and a quality; yt-dlp falls back to the next size down when a resolution disappears.
- Search: words instead of a link search YouTube and list the first results.
- Playlists: queue the first N videos of a playlist as separate rows with their own progress.
- Subscriptions: follow a channel or playlist, new videos are queued on a schedule or with Check now, each with a podcast (RSS) feed.
- Live streams: record a live stream from now until Stop, the stream ends or a time limit.
- Photo posts: Instagram, X/Twitter, Pinterest, Imgur and other galleries are saved with gallery-dl, as a zip when there is more than one file.
- Sending links in: `?url=` links, a bookmarklet, iOS Shortcuts, the Android share sheet, several links pasted at once, and Telegram.
- Private posts: add cookies in the page for login-walled Instagram, Facebook and members-only videos.
- Notifications: a webhook, Telegram messages and browser notifications when a download finishes or fails.
- Files: Keep exempts a download from cleanup (optionally into a media library folder layout), zip download for a selection, and a filter box with bulk actions.
- History: what was downloaded before survives cleanup; Download again queues it with the same options.
- Player: play finished videos, audio and photo posts in the page.
- Backup and restore: subscriptions, history, queue pause and cookies as one zip.
- Cleanup: a nightly wipe of downloads (kept rows stay) and a download window that holds queued downloads outside set hours.
- Pacing: one download per site at a time with a gap, and automatic retry of failed downloads.
- Access: an optional PIN, HTTPS with a generated certificate, and installing the page as a phone app.
- Operations: PUID/PGID set at start, a Portainer-ready compose file, a weekly image rebuild and a nightly yt-dlp self-update.
- API: a documented JSON API with live updates over Server-Sent Events.

[Unreleased]: https://github.com/jonchaka/snsdl/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/jonchaka/snsdl/releases/tag/v1.0.0

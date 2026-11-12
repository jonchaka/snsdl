# Changelog

Notable changes to snsdl. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.1.0] - 2026-11-13

### Added
- Published image `ghcr.io/jonchaka/snsdl` (amd64 and arm64), built by CI on every push to `main`, rebuilt from scratch every Monday, and tagged per release.
- Runtime `PUID`/`PGID`: the container takes ownership of the data folder at start and drops to that user, so changing the user needs no rebuild; a folder that stays unwritable stops the container with one clear log line.
- Install paths for Portainer (repository stack or app template), Unraid (template), Docker Compose with the image, and from source; `make pull` for image installs.
- The footer and `/api/status` show the build version and commit (`snsdl v1.1.0 (abc1234)`).
- This changelog, and a GitHub release created by CI from it for every `v*` tag.
- Supply chain: SBOM and provenance attestations on the image, a Trivy report on each `main` run, Dependabot for the workflow's actions, third-party actions pinned to commits.
- `SECURITY.md` with the trust model and how to report privately, and a bug-report form that asks for the footer version.

### Fixed
- `PGID` in the compose file now defaults to `PUID` instead of 1000.

## [1.0.0] - 2026-11-08

### Added
- First release on GitHub (built from source with `docker compose`).
- Downloads: paste a link, check it, pick Video (MP4), Audio (MP3) or Audio (original) and a quality; yt-dlp falls back to the next size down when a resolution disappears.
- Search: words instead of a link search YouTube and list the first results.
- Playlists: queue the first N videos of a playlist as separate rows with their own progress.
- Subscriptions: follow a channel or playlist, new videos are queued on a schedule or with Check now, each with a podcast (RSS) feed.
- Live streams: record a live stream from now until Stop, the stream ends or a time limit.
- Photo posts: Instagram, X/Twitter, Pinterest, Imgur and other galleries are saved with gallery-dl, as a zip when there is more than one file (Instagram and X need cookies).
- Sending links in: `?url=` links, a bookmarklet, iOS Shortcuts, the Android share sheet and several links pasted at once.
- Private posts: add cookies in the page for login-walled Instagram, Facebook and members-only videos.
- Notifications: a webhook, Telegram messages and browser notifications when a download finishes or fails.
- Files: Keep exempts a download from cleanup (optionally into a media library folder layout), zip download for a selection, and a filter box with bulk actions.
- History: what was downloaded before survives cleanup; Download again queues it with the same options.
- Player: play finished videos, audio and photo posts in the page.
- Backup and restore: subscriptions, history, queue pause and cookies as one zip.
- Cleanup: a nightly wipe of downloads (kept rows stay), or deletion a set number of days after each download.
- Download window: queued downloads wait outside the set hours.
- Pacing: one download per site at a time with a gap, and a single automatic retry after a network error.
- Access: an optional PIN, HTTPS with a generated certificate, and installing the page as a phone app.
- Operations: a compose file with every setting as an environment variable, an example `.env`, and a nightly yt-dlp self-update.
- API: a documented JSON API with live updates over Server-Sent Events.

[Unreleased]: https://github.com/jonchaka/snsdl/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/jonchaka/snsdl/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/jonchaka/snsdl/releases/tag/v1.0.0

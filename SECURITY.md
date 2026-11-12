# Security

## Supported versions

Only the newest image (`ghcr.io/jonchaka/snsdl:latest`, rebuilt weekly) and the newest tagged
release get fixes. Older tags are not patched.

## Trust model

snsdl is built for a trusted LAN. Keep this in mind before reporting:

- There are no user accounts. An optional `ACCESS_PIN` is the only gate, and it travels in plain
  text unless `HTTPS` is on. Exposing the app to the internet is unsupported.
- `/feed/<key>` is reachable without the PIN by design: the key in the URL is the credential.
- The server never logs or returns cookie values, the PIN, `WEBHOOK_SECRET`, a feed key or the
  Telegram token.
- The server only fetches URLs that yt-dlp or gallery-dl derive from the pasted link, the
  operator-configured webhook URL, and `api.telegram.org`. It never fetches a URL a browser
  supplies directly.
- Files are served only from the data folder and `KEEP_DIR`; cookie files and TLS keys are
  created with owner-only permissions.

Anything that breaks one of these points is a security bug.

## Reporting

Use GitHub's private vulnerability reporting:
<https://github.com/jonchaka/snsdl/security/advisories/new>. If that form is unavailable, open
an issue that says only that you have a security report and how to reach you, without details.
Please do not post exploit details publicly before a fix is released.

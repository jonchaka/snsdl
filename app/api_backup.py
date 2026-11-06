"""Backup and restore routes: GET /api/backup (the zip) and POST /api/restore."""
import email.parser
import email.policy
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response

from . import backup as backup_mod
from . import subscriptions as subs_mod
from .api_common import _require_csrf

log = logging.getLogger(__name__)

router = APIRouter()

MULTIPART_SLACK = 64 * 1024   # boundaries and part headers around the uploaded zip


@router.get("/api/backup")
async def get_backup(request: Request):
    """The backup zip (a plain link with `download`: no CSRF header, but behind the PIN)."""
    try:
        data = backup_mod.make_backup(request.app.state.settings)
    except backup_mod.BackupTooLarge as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from None
    name = f"sns-backup-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "Cache-Control": "no-store"})


async def _read_body(request: Request, limit: int) -> bytes:
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=422, detail=f"the backup is larger than "
                                                        f"{backup_mod.MAX_BACKUP // (1024 * 1024)} MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _multipart_file(content_type: str, body: bytes) -> bytes:
    """The `file` field of a multipart/form-data body (parsed with the stdlib email parser)."""
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1", "replace")
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + body)
    if msg.is_multipart():
        for part in msg.iter_parts():
            if part.get_param("name", header="content-disposition") == "file":
                return part.get_payload(decode=True) or b""
    raise HTTPException(status_code=422, detail="no file in the upload (multipart field 'file')")


@router.post("/api/restore")
async def restore(request: Request):
    """Replace subscriptions, history, the queue pause and cookies with an uploaded backup:
    multipart field `file` (or the zip as the raw body). 422 with the reason when it does not
    validate (nothing is changed then); 409 while a subscription check runs."""
    _require_csrf(request)
    # Held for the whole restore so no check starts (and saves subscriptions) halfway through.
    if not subs_mod.CHECK_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="a subscription check is running; try again when it ends")
    try:
        content_type = request.headers.get("content-type", "")
        body = await _read_body(request, backup_mod.MAX_BACKUP + MULTIPART_SLACK)
        if content_type.lower().startswith("multipart/form-data"):
            body = _multipart_file(content_type, body)
        state = request.app.state
        try:
            # Synchronous on purpose: the files are small, and running on the event loop keeps the
            # worker's own history saves and the pool's pause gate out of the way meanwhile.
            return backup_mod.restore_backup(state.settings, body, state.subscriptions, state.history,
                                             state.app_state, state.pool)
        except backup_mod.BackupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    finally:
        subs_mod.CHECK_LOCK.release()

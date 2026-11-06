"""Download history routes."""
from fastapi import APIRouter, HTTPException, Query, Request, Response

from .api_common import _require_csrf

router = APIRouter()


PREVIOUS_FIELDS = ("finished_at", "format", "quality", "title")
MAX_HISTORY_QUERY = 200


@router.get("/api/history")
async def list_history(request: Request, q: str = Query("", max_length=MAX_HISTORY_QUERY),
                       limit: int = Query(200, ge=1, le=1000)):
    history = request.app.state.history
    return {"entries": [e.to_dict() for e in history.list(q, limit)], "total": history.count()}


@router.delete("/api/history", status_code=204)
async def clear_history(request: Request):
    _require_csrf(request)
    request.app.state.history.clear()
    return Response(status_code=204)


@router.delete("/api/history/{entry_id}", status_code=204)
async def delete_history_entry(entry_id: str, request: Request):
    _require_csrf(request)
    if not request.app.state.history.remove(entry_id):
        raise HTTPException(status_code=404)
    return Response(status_code=204)

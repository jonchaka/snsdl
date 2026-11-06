"""The HTTP API: one router that includes the per-resource routers.

Re-exports the names other modules and tests import from `app.api`. These are compatibility
bindings only: `app.api.X` is a separate name bound to the owning module's object, so patching
it changes nothing the routes see. Tests must monkeypatch the owning module (`app.api_jobs`,
`app.api_system`, ...)."""
from fastapi import APIRouter

from .api_common import CSRF_HEADER, CSRF_VALUE, _file_url, _human
from .api_history import router as history_router
from .api_jobs import _content_disposition, _zip_filename
from .api_jobs import router as jobs_router
from .api_subscriptions import router as subscriptions_router
from .api_system import AUTH_FAIL_DELAY, downloads_usage, status_payload
from .api_system import router as system_router

__all__ = ["router", "status_payload", "downloads_usage", "_file_url", "_human", "_zip_filename",
           "_content_disposition", "CSRF_HEADER", "CSRF_VALUE", "AUTH_FAIL_DELAY"]

router = APIRouter()
router.include_router(jobs_router)
router.include_router(subscriptions_router)
router.include_router(history_router)
router.include_router(system_router)

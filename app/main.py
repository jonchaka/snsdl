import asyncio
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import auth, tls
from .api import router
from .cleanup import build_scheduler, empty_dir, sweep_thumbs
from .config import Settings
from .jobs import JobStore
from .state import AppState
from .updater import Updater
from .worker import WorkerPool

STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, run_background: bool = True) -> FastAPI:
    """App factory. `run_background=False` skips workers and the scheduler (for tests)."""
    settings = settings or Settings.from_env()
    settings.ensure_dirs()

    store = JobStore(settings.jobs_file)
    store.load()
    app_state = AppState(settings.state_file)
    app_state.load()
    pool = WorkerPool(store, settings)
    if app_state.queue_paused:
        pool.pause()
    updater = Updater(settings, pool, store)
    updater.check()

    async def scheduled_update() -> None:
        await asyncio.to_thread(updater.update)
        updater.schedule_restart()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        empty_dir(settings.tmp_dir)
        for job in store.recover_after_restart():
            pool.enqueue(job.id)
        sweep_thumbs(store, settings)   # once, before any worker can fetch a new one
        scheduler = None
        if run_background:
            await pool.start()
            scheduler = build_scheduler(store, settings, update_job=scheduled_update)
            scheduler.start()
            log.info("started %d workers; cleanup cron %r, update cron %r (%s); yt-dlp %s",
                     settings.max_concurrent, settings.cleanup_cron, settings.update_cron or "off",
                     settings.tz, updater.status.running)
            if updater.status.error:
                log.warning("yt-dlp update: %s", updater.status.error)
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)
            if run_background:
                await pool.stop()

    app = FastAPI(title="SNS Downloader", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.pool = pool
    app.state.app_state = app_state
    app.state.updater = updater
    app.state.auth_limiter = auth.FailureLimiter()
    app.state.secret = None
    # The leaf's SAN list, read once: the page warns when it is opened by another name.
    app.state.cert_hosts = tls.san_hosts(settings.tls_dir / tls.SERVER_CERT) if settings.https else []
    if settings.access_pin:
        app.state.secret = auth.secret_for(settings)   # never log it
        app.add_middleware(auth.AuthMiddleware, settings=settings)
    app.include_router(router)
    # Not in every platform's mime table; the manifest and icons must have the right type.
    mimetypes.add_type("application/manifest+json", ".webmanifest")
    mimetypes.add_type("image/svg+xml", ".svg")
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app

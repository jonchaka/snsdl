import asyncio
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import auth, events, feed, subscriptions, tls, webhook
from .api import router
from .cleanup import build_scheduler, empty_dir, sweep_thumbs
from .config import Settings
from .history import HistoryStore
from .jobs import JobStore
from .state import AppState
from .subscriptions import SubscriptionStore
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
    subs = SubscriptionStore(settings.subscriptions_file)
    subs.load()
    history = HistoryStore(settings.history_file)
    history.load()
    pending = webhook.PendingStore(settings.webhook_pending_file)
    pool = WorkerPool(store, settings, history=history, subs=subs, pending=pending)
    broadcaster = events.Broadcaster()
    feed.install_log_filter()   # feed URLs carry their key: keep it out of the access log
    store.add_listener(broadcaster.notify)   # only schedules a wake-up on the loop
    if app_state.queue_paused:
        pool.pause()
    updater = Updater(settings, pool, store)
    updater.check()

    async def scheduled_update() -> None:
        await asyncio.to_thread(updater.update)
        updater.schedule_restart()

    async def scheduled_subscriptions() -> None:
        # Module attribute looked up at call time so tests can patch check_all.
        await asyncio.to_thread(subscriptions.check_all, subs, store, pool, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        broadcaster.bind(asyncio.get_running_loop())
        empty_dir(settings.tmp_dir)
        for job in store.recover_after_restart():
            pool.enqueue(job.id)
        sweep_thumbs(store, settings, history)   # once, before any worker can fetch a new one
        scheduler = None
        if run_background:
            await pool.start()
            scheduler = build_scheduler(store, settings, update_job=scheduled_update,
                                        subscription_job=scheduled_subscriptions, history=history)
            scheduler.start()
            log.info("started %d workers; cleanup cron %r, update cron %r, subscriptions cron %r (%s); "
                     "yt-dlp %s", settings.max_concurrent, settings.cleanup_cron,
                     settings.update_cron or "off", settings.subscription_cron or "off",
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
    app.state.subscriptions = subs
    app.state.history = history
    app.state.events = broadcaster
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

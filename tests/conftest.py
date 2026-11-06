import pytest
from fastapi.testclient import TestClient

from app import images as images_mod
from app import worker as worker_mod
from app.config import Settings
from app.jobs import JobStore
from app.main import create_app


@pytest.fixture(autouse=True)
def no_gallery_dl_network(monkeypatch):
    """No test reaches gallery-dl's extractors (they would go to the network): by default no
    URL is an image post. tests/test_images.py injects its own fakes."""
    monkeypatch.setattr(images_mod, "find", lambda url: None)


@pytest.fixture
def settings(tmp_path):
    s = Settings.from_env({"DATA_DIR": str(tmp_path)})
    s.ensure_dirs()
    return s


@pytest.fixture
def store(settings):
    s = JobStore(settings.jobs_file)
    s.load()
    return s


@pytest.fixture
def client(settings, monkeypatch):
    # The 507 guard reads the real disk; keep tests independent of the machine's free space.
    monkeypatch.setattr(worker_mod, "disk_low", lambda s: False)
    app = create_app(settings, run_background=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def keep_dir(settings):
    settings.keep_dir = settings.data_dir / "keep"
    settings.keep_dir.mkdir()
    return settings.keep_dir

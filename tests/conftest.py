import pytest

from app.config import Settings
from app.jobs import JobStore


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

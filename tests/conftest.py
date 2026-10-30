import pytest

from app.config import Settings


@pytest.fixture
def settings(tmp_path):
    s = Settings.from_env({"DATA_DIR": str(tmp_path)})
    s.ensure_dirs()
    return s

import os
import shutil
import tempfile
from pathlib import Path

# Must be set before the app is imported: tests use a throw-away DB and storage dir.
_TMP = Path(tempfile.mkdtemp(prefix="certgen-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ["MAX_RECIPIENTS_PER_JOB"] = "50"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    """Fresh tables and an empty storage directory for every test."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    shutil.rmtree(settings.storage_dir, ignore_errors=True)
    settings.storage_dir.mkdir(parents=True)
    yield


@pytest.fixture
def client():
    # Not used as a context manager, so the startup hook is skipped; clean_state handles setup.
    # Note: TestClient runs FastAPI background tasks before the call returns, which makes
    # the "background" generation deterministic in tests.
    return TestClient(app)

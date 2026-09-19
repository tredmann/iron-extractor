from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

API_KEY = "test-key"


class RecordingService:
    """A ConversionService stand-in that writes canned artifacts instantly."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.raises: Exception | None = None
        self.manifest: dict | None = None

    def run(self, job_id, input_path, options):
        self.calls.append((job_id, input_path, options))
        if self.raises is not None:
            raise self.raises
        return self.manifest if self.manifest is not None else {
            "page_count": 1,
            "warnings": [],
            "markdown": {"bytes": 5},
            "document": {"bytes": 2},
            "tables": [],
            "images": [],
        }


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(api_keys=API_KEY, data_dir=tmp_path, worker_threads=1, queue_max=4)


@pytest.fixture
def recording_service() -> RecordingService:
    return RecordingService()


@pytest.fixture
def client(settings: Settings, recording_service: RecordingService):
    app = create_app(settings=settings, service=recording_service)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth() -> dict:
    return {"X-API-Key": API_KEY}

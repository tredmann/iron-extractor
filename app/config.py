from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings(BaseSettings):
    """Runtime configuration, read from the environment."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_keys: str
    data_dir: Path = Path("/data")
    models_dir: Path | None = None

    worker_threads: int = 2
    queue_max: int = 32
    job_timeout_seconds: float = 600.0
    converter_cache_size: int = 4
    shutdown_grace_seconds: float = 30.0
    max_upload_bytes: int = 200 * 1024 * 1024

    default_ocr: bool = True
    default_ocr_languages: str = "en"
    default_table_mode: str = "accurate"
    default_image_scale: float = 2.0

    docs_enabled: bool = True
    log_level: str = "INFO"

    @property
    def api_key_list(self) -> list[str]:
        return _split_csv(self.api_keys)

    @property
    def default_ocr_language_list(self) -> list[str]:
        return _split_csv(self.default_ocr_languages)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"


@lru_cache
def get_settings() -> Settings:
    return Settings()

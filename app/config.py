from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TableMode(str, Enum):
    """TableFormer modes this server understands.

    Defined here, in the lowest module of the import graph, because both
    `Settings` and the request schema need it: `app.api.schemas` imports
    `Settings`, so the enum cannot live there without a cycle. Re-exported
    from `app.api.schemas` so the API layer stays the place to import it
    from.
    """

    FAST = "fast"
    ACCURATE = "accurate"


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings(BaseSettings):
    """Runtime configuration, read from the environment.

    Constraints are declared rather than papered over downstream. A bad
    `DEFAULT_TABLE_MODE` used to sail through startup and then make *every*
    upload fail with `400 "options.table_mode: Input should be 'fast' or
    'accurate'"` -- blaming the caller for the operator's typo. Sizes were
    coerced with `max(1, ...)` where they were used, so `QUEUE_MAX=0`
    silently became an unbounded queue. Both now fail the process at
    startup, where a misconfiguration belongs.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_keys: str
    data_dir: Path = Path("/data")
    models_dir: Path | None = None

    worker_threads: int = Field(default=2, gt=0)
    queue_max: int = Field(default=32, gt=0)
    job_timeout_seconds: float = 600.0
    converter_cache_size: int = Field(default=4, gt=0)
    shutdown_grace_seconds: float = 30.0
    max_upload_bytes: int = Field(default=200 * 1024 * 1024, gt=0)

    default_ocr: bool = True
    default_ocr_languages: str = "en"
    default_table_mode: TableMode = TableMode.ACCURATE
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

"""Runtime configuration, read from environment variables."""
import os
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        self.database_url: str = os.getenv("DATABASE_URL", "sqlite:///./certificates.db")
        self.storage_dir: Path = Path(os.getenv("STORAGE_DIR", "./storage"))
        # Upper bound for one request; keeps request bodies and DB inserts bounded.
        self.max_recipients: int = int(os.getenv("MAX_RECIPIENTS_PER_JOB", "5000"))


settings = Settings()

import os
import secrets
from pathlib import Path


class Settings:
    secret_key: str = os.getenv("APP_SECRET_KEY", secrets.token_urlsafe(32))
    username: str = os.getenv("FUND_LEDGER_USERNAME", "admin")
    password_hash: str = os.getenv(
        "FUND_LEDGER_PASSWORD_HASH",
        "$2b$12$.F8VDZ2aTHPmBtR1XJEwsOS2W1AfpZFUumyJku6KtWdzRcb1MZaxm",
    )
    data_dir: Path = Path(os.getenv("FUND_LEDGER_DATA_DIR", "/www/data/fund-ledger"))
    db_path: Path = Path(
        os.getenv("FUND_LEDGER_DB", "/www/data/fund-ledger/fund-ledger.sqlite3")
    )

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def nav_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "nav"


settings = Settings()


def ensure_data_dirs() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.nav_cache_dir.mkdir(parents=True, exist_ok=True)

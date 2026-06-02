from collections.abc import Generator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from .config import ensure_data_dirs, settings


engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    ensure_data_dirs()
    SQLModel.metadata.create_all(engine)
    migrate_schema()


def migrate_schema() -> None:
    with engine.begin() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        if "fundrule" not in tables:
            return
        columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(fundrule)"))
        }
        if "sync_source" not in columns:
            connection.execute(text("ALTER TABLE fundrule ADD COLUMN sync_source TEXT DEFAULT ''"))
        if "synced_at" not in columns:
            connection.execute(text("ALTER TABLE fundrule ADD COLUMN synced_at TIMESTAMP"))


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session

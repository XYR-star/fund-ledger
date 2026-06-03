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
        if "benchmarknav" not in tables:
            connection.execute(
                text(
                    """
                    CREATE TABLE benchmarknav (
                        id INTEGER PRIMARY KEY,
                        benchmark_code VARCHAR(32) NOT NULL,
                        benchmark_name VARCHAR NOT NULL DEFAULT '',
                        nav_date DATE NOT NULL,
                        close_value FLOAT NOT NULL,
                        source VARCHAR NOT NULL DEFAULT 'akshare',
                        created_at TIMESTAMP NOT NULL,
                        updated_at TIMESTAMP NOT NULL,
                        UNIQUE (benchmark_code, nav_date)
                    )
                    """
                )
            )
            connection.execute(text("CREATE INDEX ix_benchmarknav_benchmark_code ON benchmarknav (benchmark_code)"))
            connection.execute(text("CREATE INDEX ix_benchmarknav_nav_date ON benchmarknav (nav_date)"))


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session

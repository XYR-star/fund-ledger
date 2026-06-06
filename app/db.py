from collections.abc import Generator

from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine

from .config import ensure_data_dirs, settings


engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


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
        if "fund_type" not in columns:
            connection.execute(text("ALTER TABLE fundrule ADD COLUMN fund_type TEXT DEFAULT ''"))
        if "fundtransactioncandidate" in tables:
            candidate_columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(fundtransactioncandidate)"))
            }
            if "submitted_at" not in candidate_columns:
                connection.execute(text("ALTER TABLE fundtransactioncandidate ADD COLUMN submitted_at TIME"))
        if "fundtransaction" in tables:
            transaction_columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(fundtransaction)"))
            }
            if "submitted_at" not in transaction_columns:
                connection.execute(text("ALTER TABLE fundtransaction ADD COLUMN submitted_at TIME"))
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
        if "fundalias" not in tables:
            connection.execute(
                text(
                    """
                    CREATE TABLE fundalias (
                        id INTEGER PRIMARY KEY,
                        pattern VARCHAR NOT NULL,
                        replacement VARCHAR NOT NULL DEFAULT '',
                        notes VARCHAR NOT NULL DEFAULT '',
                        created_at TIMESTAMP NOT NULL,
                        updated_at TIMESTAMP NOT NULL
                    )
                    """
                )
            )
            connection.execute(text("CREATE INDEX ix_fundalias_pattern ON fundalias (pattern)"))
        if "operationaudit" not in tables:
            connection.execute(
                text(
                    """
                    CREATE TABLE operationaudit (
                        id INTEGER PRIMARY KEY,
                        action VARCHAR NOT NULL,
                        target_type VARCHAR NOT NULL,
                        target_id VARCHAR NOT NULL DEFAULT '',
                        detail VARCHAR NOT NULL DEFAULT '',
                        created_at TIMESTAMP NOT NULL
                    )
                    """
                )
            )
            connection.execute(text("CREATE INDEX ix_operationaudit_action ON operationaudit (action)"))
            connection.execute(text("CREATE INDEX ix_operationaudit_target_type ON operationaudit (target_type)"))
            connection.execute(text("CREATE INDEX ix_operationaudit_created_at ON operationaudit (created_at)"))


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session

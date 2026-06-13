from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from .config import settings


database_url = f"sqlite:///{settings.db_path}"
engine = create_engine(
    database_url,
    connect_args={"check_same_thread": False, "timeout": 30},
    echo=False,
)


@event.listens_for(engine, "connect")
def configure_sqlite(dbapi_connection, _):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    migrate_eaccount_holding_schema()
    cleanup_invalid_eaccount_holdings()


def migrate_eaccount_holding_schema() -> None:
    table = SQLModel.metadata.tables.get("eaccountholding")
    if table is None:
        return
    expected = {column.name for column in table.columns}
    with engine.begin() as connection:
        existing_rows = connection.exec_driver_sql("PRAGMA table_info(eaccountholding)").fetchall()
        if not existing_rows:
            return
        existing = {row[1] for row in existing_rows}
        if existing == expected:
            return
        temp_name = "eaccountholding_migration_old"
        connection.exec_driver_sql(f"DROP TABLE IF EXISTS {temp_name}")
        indexes = connection.exec_driver_sql("PRAGMA index_list(eaccountholding)").fetchall()
        for index in indexes:
            index_name = index[1]
            if index_name:
                connection.exec_driver_sql(f"DROP INDEX IF EXISTS {index_name}")
        connection.exec_driver_sql(f"ALTER TABLE eaccountholding RENAME TO {temp_name}")
        table.create(connection)
        common = [column.name for column in table.columns if column.name in existing]
        if common:
            column_list = ", ".join(common)
            connection.exec_driver_sql(
                f"INSERT INTO eaccountholding ({column_list}) SELECT {column_list} FROM {temp_name}"
            )
        connection.exec_driver_sql(f"DROP TABLE {temp_name}")


def cleanup_invalid_eaccount_holdings() -> None:
    with engine.begin() as connection:
        holding_exists = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='eaccountholding'"
        ).fetchone()
        import_exists = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='eaccountimport'"
        ).fetchone()
        if not holding_exists or not import_exists:
            return
        connection.exec_driver_sql(
            """
            DELETE FROM eaccountholding
            WHERE fund_code IN ('', '000000')
              AND lower(coalesce(fund_name, '')) IN ('', 'nan', 'nat', 'none', 'null')
              AND official_share IS NULL
              AND official_market_value IS NULL
              AND settlement_value IS NULL
            """
        )
        connection.exec_driver_sql(
            """
            UPDATE eaccountimport
            SET row_count = (
                    SELECT COUNT(*) FROM eaccountholding
                    WHERE eaccountholding.import_id = eaccountimport.id
                ),
                matched_count = (
                    SELECT COUNT(*) FROM eaccountholding
                    WHERE eaccountholding.import_id = eaccountimport.id
                      AND eaccountholding.status = 'matched'
                ),
                mismatch_count = (
                    SELECT COUNT(*) FROM eaccountholding
                    WHERE eaccountholding.import_id = eaccountimport.id
                      AND eaccountholding.status = 'mismatch'
                ),
                missing_count = (
                    SELECT COUNT(*) FROM eaccountholding
                    WHERE eaccountholding.import_id = eaccountimport.id
                      AND eaccountholding.status = 'missing'
                )
            """
        )


def get_session() -> Session:
    with Session(engine) as session:
        yield session

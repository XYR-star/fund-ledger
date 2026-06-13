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


def get_session() -> Session:
    with Session(engine) as session:
        yield session

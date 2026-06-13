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
    merge_existing_eaccount_holdings()


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


def merge_existing_eaccount_holdings() -> None:
    with engine.begin() as connection:
        holding_exists = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='eaccountholding'"
        ).fetchone()
        import_exists = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='eaccountimport'"
        ).fetchone()
        if not holding_exists or not import_exists:
            return
        groups = connection.exec_driver_sql(
            """
            SELECT import_id, fund_code, coalesce(share_date, nav_date, '') AS snapshot_date, COUNT(*) AS row_count
            FROM eaccountholding
            WHERE fund_code NOT IN ('', '000000')
            GROUP BY import_id, fund_code, coalesce(share_date, nav_date, '')
            HAVING row_count > 1
            """
        ).fetchall()
        for import_id, fund_code, snapshot_date, _ in groups:
            rows = connection.exec_driver_sql(
                """
                SELECT id, fund_name, fund_account, official_share, share_date, nav, nav_date,
                       official_market_value, settlement_value, local_share, local_market_value
                FROM eaccountholding
                WHERE import_id = ? AND fund_code = ? AND coalesce(share_date, nav_date, '') = ?
                ORDER BY id
                """,
                (import_id, fund_code, snapshot_date),
            ).mappings().fetchall()
            if len(rows) < 2:
                continue
            first = rows[0]
            keep_id = first["id"]
            official_share = sum_optional(row["official_share"] for row in rows)
            official_market_value = sum_optional(row["official_market_value"] for row in rows)
            settlement_value = sum_optional(row["settlement_value"] for row in rows)
            local_share = first["local_share"]
            local_market_value = first["local_market_value"]
            official_market = settlement_value if settlement_value is not None else official_market_value
            share_diff = round(official_share - local_share, 2) if official_share is not None and local_share is not None else None
            market_diff = round(official_market - local_market_value, 2) if official_market is not None and local_market_value is not None else None
            issues = []
            if local_share is None:
                issues.append("系统缺少持仓")
            if share_diff is not None and abs(share_diff) > 0.02:
                issues.append(f"份额差异 {share_diff:.2f}")
            if market_diff is not None and abs(market_diff) > 1.0:
                issues.append(f"市值差异 {market_diff:.2f}")
            status = "missing" if local_share is None else ("mismatch" if issues else "matched")
            issue_summary = "；".join(issues) if issues else "匹配"
            connection.exec_driver_sql(
                """
                UPDATE eaccountholding
                SET fund_name = ?, fund_account = ?, official_share = ?, official_market_value = ?,
                    settlement_value = ?, share_diff = ?, market_value_diff = ?, status = ?, issue_summary = ?
                WHERE id = ?
                """,
                (
                    first["fund_name"] or "",
                    first["fund_account"] or "",
                    official_share,
                    official_market_value,
                    settlement_value,
                    share_diff,
                    market_diff,
                    status,
                    issue_summary,
                    keep_id,
                ),
            )
            delete_ids = [row["id"] for row in rows[1:]]
            placeholders = ", ".join("?" for _ in delete_ids)
            connection.exec_driver_sql(f"DELETE FROM eaccountholding WHERE id IN ({placeholders})", tuple(delete_ids))
        refresh_eaccount_import_counts(connection)


def sum_optional(values) -> float | None:
    numbers = [value for value in values if value is not None]
    if not numbers:
        return None
    return round(sum(numbers), 2)


def refresh_eaccount_import_counts(connection) -> None:
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

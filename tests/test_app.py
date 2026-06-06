from datetime import date, datetime
import asyncio
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlmodel import Session, SQLModel, select


pytestmark = pytest.mark.anyio


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture()
async def client(tmp_path, monkeypatch):
    db_path = tmp_path / "fund-ledger.sqlite3"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")
    monkeypatch.setenv("FUND_LEDGER_USERNAME", "admin")
    monkeypatch.setenv(
        "FUND_LEDGER_PASSWORD_HASH",
        "$2b$12$.F8VDZ2aTHPmBtR1XJEwsOS2W1AfpZFUumyJku6KtWdzRcb1MZaxm",
    )
    monkeypatch.setenv("FUND_LEDGER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("FUND_LEDGER_DB", str(db_path))

    import importlib
    import app.config
    import app.db
    import app.main
    from app.fund_rule_sync import SyncedRule

    importlib.reload(app.config)
    importlib.reload(app.db)
    importlib.reload(app.main)
    monkeypatch.setattr(app.main, "sync_nav_for_fund", lambda *_: (0, "offline"))
    monkeypatch.setattr(app.main, "sync_hs300", lambda *_: (0, None))
    monkeypatch.setattr(
        app.main,
        "fetch_fund_rule_from_akshare",
        lambda code: SyncedRule(
            fund_code=code,
            fund_name="",
            buy_confirm_days=1,
            sell_confirm_days=1,
            buy_fee_rate=0.0,
            fee_tiers=[],
            source="test",
        ),
    )
    SQLModel.metadata.drop_all(app.db.engine)
    SQLModel.metadata.create_all(app.db.engine)
    transport = httpx.ASGITransport(app=app.main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as test_client:
        yield test_client


async def login(client):
    return await client.post(
        "/login",
        data={"username": "admin", "password": "changeme", "next": "/"},
        follow_redirects=False,
    )


async def wait_for_text(client, path, text, attempts=60):
    response = None
    for _ in range(attempts):
        response = await client.get(path)
        if text in response.text:
            return response
        await asyncio.sleep(0.05)
    return response


async def test_requires_login(client):
    response = await client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_login_page_has_theme_toggle(client):
    page = await client.get("/login")
    assert 'data-theme-toggle' in page.text
    assert 'fund-ledger-theme' in page.text
    assert 'theme-icon-moon' in page.text
    assert 'theme-icon-sun' in page.text


async def test_login_success(client):
    response = await login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


async def test_theme_toggle_is_available_on_pages(client):
    await login(client)
    page = await client.get("/")
    assert 'data-theme-toggle' in page.text
    assert 'fund-ledger-theme' in page.text
    assert 'prefers-color-scheme: dark' in page.text
    assert 'aria-pressed' in page.text


async def test_candidate_confirm_flow(client):
    await login(client)
    response = await client.post(
        "/upload",
        data={
            "raw_text": "2024-01-02 161725 招商中证白酒 buy 1000 - 1.0000 1.00"
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    page = await wait_for_text(client, "/candidates", "161725")
    assert "161725" in page.text
    assert "pending" in page.text

    response = await client.post("/candidates/1/confirm", follow_redirects=False)
    assert response.status_code == 303
    tx_page = await client.get("/transactions")
    assert "招商中证白酒" in tx_page.text
    assert "¥1000.00" in tx_page.text

    response = await client.post("/candidates/1/confirm", follow_redirects=False)
    assert response.status_code == 303
    tx_page = await client.get("/transactions")
    assert tx_page.text.count("招商中证白酒") == 1


async def test_confirm_all_deduplicates_identical_source_candidates(client):
    import app.db
    from app.models import CandidateStatus, FundTransaction, FundTransactionCandidate, TransactionAction

    await login(client)
    with Session(app.db.engine) as session:
        for _ in range(2):
            session.add(
                FundTransactionCandidate(
                    status=CandidateStatus.pending,
                    fund_code="161725",
                    fund_name="招商中证白酒",
                    trade_date=date(2024, 1, 2),
                    action=TransactionAction.buy,
                    amount_cny=100,
                    share=50,
                    nav=2,
                    source_file="/tmp/same.png",
                )
            )
        session.commit()

    response = await client.post("/candidates/confirm-all", follow_redirects=False)
    assert response.status_code == 303
    with Session(app.db.engine) as session:
        transactions = session.exec(select(FundTransaction)).all()
        candidates = session.exec(select(FundTransactionCandidate)).all()
    assert len(transactions) == 1
    assert len(candidates) == 2
    assert {candidate.status for candidate in candidates} == {CandidateStatus.confirmed}
    assert {candidate.confirmed_transaction_id for candidate in candidates} == {transactions[0].id}


async def test_candidates_page_marks_duplicate_pending_candidates(client):
    import app.db
    from app.models import CandidateStatus, FundTransactionCandidate, TransactionAction

    await login(client)
    with Session(app.db.engine) as session:
        for _ in range(2):
            session.add(
                FundTransactionCandidate(
                    status=CandidateStatus.pending,
                    fund_code="161725",
                    fund_name="招商中证白酒",
                    trade_date=date(2024, 1, 2),
                    action=TransactionAction.buy,
                    amount_cny=100,
                    share=50,
                    nav=2,
                    source_file="/tmp/same.png",
                )
            )
        session.commit()

    page = await client.get("/candidates")
    assert page.text.count("疑似重复") == 2


async def test_candidates_page_filters_by_import_and_confirm_all_scope(client):
    import app.db
    from app.models import CandidateStatus, FundTransaction, FundTransactionCandidate, ImportDocument, ImportStatus, TransactionAction

    await login(client)
    with Session(app.db.engine) as session:
        first = ImportDocument(file_name="one.png", source_hash="hash-one", status=ImportStatus.parse_done)
        second = ImportDocument(file_name="two.png", source_hash="hash-two", status=ImportStatus.parse_done)
        session.add(first)
        session.add(second)
        session.commit()
        session.add(
            FundTransactionCandidate(
                status=CandidateStatus.pending,
                fund_code="161725",
                fund_name="招商中证白酒",
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=50,
                nav=2,
                source_hash="hash-one",
            )
        )
        session.add(
            FundTransactionCandidate(
                status=CandidateStatus.pending,
                fund_code="005827",
                fund_name="易方达蓝筹",
                trade_date=date(2024, 1, 3),
                action=TransactionAction.buy,
                amount_cny=200,
                share=100,
                nav=2,
                source_hash="hash-two",
            )
        )
        session.add(
            FundTransactionCandidate(
                status=CandidateStatus.pending,
                fund_code="000000",
                fund_name="未知基金",
                trade_date=date(2024, 1, 4),
                action=TransactionAction.buy,
                amount_cny=300,
                source_hash="hash-one",
            )
        )
        session.commit()

    page = await client.get("/candidates?source_hash=hash-one")
    assert "one.png" in page.text
    assert "161725" in page.text
    assert "005827" not in page.text
    assert "未知基金" in page.text

    unmatched = await client.get("/candidates?source_hash=hash-one&unmatched=1")
    assert "未知基金" in unmatched.text
    assert "161725" not in unmatched.text

    response = await client.post(
        "/candidates/confirm-all",
        data={"source_hash": "hash-one", "return_to": "/candidates?source_hash=hash-one"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "source_hash=hash-one" in response.headers["location"]

    with Session(app.db.engine) as session:
        transactions = session.exec(select(FundTransaction)).all()
        candidates = session.exec(select(FundTransactionCandidate)).all()
    assert len(transactions) == 1
    assert transactions[0].fund_code == "161725"
    assert {candidate.fund_code: candidate.status for candidate in candidates}["005827"] == CandidateStatus.pending


async def test_auto_confirm_safe_only_confirms_high_quality_candidates(client):
    import app.db
    from app.main import create_candidates_from_rows
    from app.models import CandidateStatus, FundNav, FundTransaction, FundTransactionCandidate, ImportDocument, ImportStatus

    await login(client)
    with Session(app.db.engine) as session:
        session.add(ImportDocument(file_name="quality.png", source_hash="quality-source", status=ImportStatus.parse_done))
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 2), unit_nav=2.0))
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 3), unit_nav=2.1))
        create_candidates_from_rows(
            session,
            [
                {
                    "fund_code": "161725",
                    "fund_name": "招商中证白酒",
                    "trade_date": "2024-01-02",
                    "action": "buy",
                    "amount_cny": 100,
                },
                {
                    "fund_code": "000000",
                    "fund_name": "未知基金",
                    "trade_date": "2024-01-02",
                    "action": "buy",
                    "amount_cny": 200,
                },
            ],
            source_hash="quality-source",
        )
        session.commit()

    page = await client.get("/candidates?source_hash=quality-source")
    assert "质量 高" in page.text
    assert "自动确认高质量" in page.text
    auto_page = await client.get("/candidates?source_hash=quality-source&quality=auto")
    assert "161725" in auto_page.text
    assert "未知基金" not in auto_page.text
    review_page = await client.get("/candidates?source_hash=quality-source&quality=review")
    assert "未知基金" in review_page.text
    assert "161725" not in review_page.text
    imports_page = await client.get("/imports")
    assert "quality=auto" in imports_page.text

    response = await client.post(
        "/candidates/auto-confirm-safe",
        data={"source_hash": "quality-source", "return_to": "/candidates?source_hash=quality-source"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "source_hash=quality-source" in response.headers["location"]

    with Session(app.db.engine) as session:
        transactions = session.exec(select(FundTransaction)).all()
        candidates = session.exec(select(FundTransactionCandidate).order_by(FundTransactionCandidate.id)).all()
    assert len(transactions) == 1
    assert transactions[0].fund_code == "161725"
    assert candidates[0].status == CandidateStatus.confirmed
    assert candidates[1].status == CandidateStatus.pending


async def test_ignore_candidate_does_not_create_transaction(client):
    await login(client)
    await client.post(
        "/upload",
        data={"raw_text": "2024-01-02 005827 易方达蓝筹 buy 500 - 2.0000 0"},
    )
    await wait_for_text(client, "/candidates", "005827")
    await client.post("/candidates/1/ignore", follow_redirects=False)
    tx_page = await client.get("/transactions")
    assert "易方达蓝筹" not in tx_page.text


async def test_manual_transaction_create_and_delete(client):
    import app.db
    from app.models import FundRule, FundTransaction

    await login(client)
    with Session(app.db.engine) as session:
        session.add(FundRule(fund_code="161725", fund_name="招商中证白酒", buy_fee_rate=0.0))
        session.commit()

    response = await client.post(
        "/transactions",
        data={
            "fund_code": "161725",
            "fund_name": "",
            "trade_date": "2024-01-02",
            "action": "buy",
            "amount_cny": "1000",
            "nav": "2.0",
            "note": "手动补录",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = await client.get("/transactions")
    assert "招商中证白酒" in page.text
    assert "手动补录" in page.text
    assert "500.0000" in page.text

    holdings = await client.get("/holdings")
    assert "161725" in holdings.text
    assert "500.0000" in holdings.text

    with Session(app.db.engine) as session:
        tx = session.exec(select(FundTransaction)).first()
        tx_id = tx.id
    response = await client.post(f"/transactions/{tx_id}/delete", follow_redirects=False)
    assert response.status_code == 303
    page = await client.get("/transactions")
    assert "招商中证白酒" not in page.text
    holdings = await client.get("/holdings")
    assert "161725" not in holdings.text


async def test_transactions_filter_and_edit_updates_holdings(client):
    import app.db
    from app.models import FundRule, FundTransaction, TransactionAction

    await login(client)
    with Session(app.db.engine) as session:
        session.add(FundRule(fund_code="161725", fund_name="招商中证白酒", buy_fee_rate=0.0))
        session.add(FundRule(fund_code="005827", fund_name="易方达蓝筹", buy_fee_rate=0.0))
        session.add(
            FundTransaction(
                fund_code="161725",
                fund_name="招商中证白酒",
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=1000,
                share=500,
                nav=2,
                source_file="manual",
                raw_text="old note",
            )
        )
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹",
                trade_date=date(2024, 1, 3),
                action=TransactionAction.buy,
                amount_cny=200,
                share=100,
                nav=2,
                source_file="manual",
            )
        )
        session.commit()
        tx_id = session.exec(select(FundTransaction).where(FundTransaction.fund_code == "161725")).first().id

    page = await client.get("/transactions?fund_code=161725&action=buy&date_from=2024-01-01&date_to=2024-01-02")
    assert "招商中证白酒" in page.text
    assert "易方达蓝筹" not in page.text

    response = await client.post(
        f"/transactions/{tx_id}/update",
        data={
            "return_to": "/transactions?fund_code=161725",
            "fund_code": "161725",
            "fund_name": "招商中证白酒",
            "trade_date": "2024-01-02",
            "action": "buy",
            "amount_cny": "1200",
            "nav": "2.0",
            "fee": "0",
            "note": "edited note",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "fund_code=161725" in response.headers["location"]
    page = await client.get("/transactions?fund_code=161725")
    assert "edited note" in page.text
    assert "600.0" in page.text
    holdings = await client.get("/holdings")
    assert "600.0000" in holdings.text


async def test_holdings_calculation_without_synced_nav(client):
    await login(client)
    await client.post(
        "/upload",
        data={"raw_text": "2024-01-02 005827 易方达蓝筹 buy 500 250 2.0000 0"},
    )
    await wait_for_text(client, "/candidates", "005827")
    await client.post("/candidates/1/confirm", follow_redirects=False)
    page = await client.get("/holdings")
    assert "005827" in page.text
    assert "250.0000" in page.text
    assert "¥500.00" in page.text


async def test_performance_page_draws_fund_and_benchmark_curves(client):
    import app.db
    from app.models import BenchmarkNav, FundNav

    await login(client)
    await client.post(
        "/upload",
        data={"raw_text": "2024-01-02 005827 易方达蓝筹 buy 500 250 2.0000 0"},
        follow_redirects=False,
    )
    await wait_for_text(client, "/candidates", "005827")
    await client.post("/candidates/1/confirm", follow_redirects=False)
    with Session(app.db.engine) as session:
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 2), unit_nav=2.0))
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 3), unit_nav=2.2))
        session.add(
            BenchmarkNav(
                benchmark_code="000300",
                benchmark_name="沪深300",
                nav_date=date(2024, 1, 2),
                close_value=3300,
            )
        )
        session.add(
            BenchmarkNav(
                benchmark_code="000300",
                benchmark_name="沪深300",
                nav_date=date(2024, 1, 3),
                close_value=3366,
            )
        )
        session.commit()

    page = await client.get("/performance")
    assert page.status_code == 200
    assert "收益曲线" in page.text
    assert "005827" in page.text
    assert "沪深300" in page.text
    assert "fund-line" in page.text
    assert "benchmark-line" in page.text
    assert "buy-marker" in page.text
    assert "10.0%" in page.text


async def test_closed_position_moves_to_closed_section_and_skips_main_curve(client):
    import app.db
    from app.models import FundNav, FundTransaction, TransactionAction

    await login(client)
    with Session(app.db.engine) as session:
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹",
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=1000,
                share=500,
                nav=2.0,
                fee=0,
            )
        )
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹",
                trade_date=date(2024, 1, 3),
                action=TransactionAction.sell,
                amount_cny=1100,
                share=500,
                nav=2.2,
                fee=0,
            )
        )
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 2), unit_nav=2.0))
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 3), unit_nav=2.2))
        session.commit()

    holdings = await client.get("/holdings")
    assert "已清仓" in holdings.text
    assert "¥100.00" in holdings.text
    assert "10.00%" in holdings.text
    assert 'href="/holdings/005827"' in holdings.text

    performance = await client.get("/performance")
    assert "005827" not in performance.text


async def test_holding_detail_shows_buy_and_sell_markers(client):
    import app.db
    from app.models import BenchmarkNav, FundNav, FundTransaction, TransactionAction

    await login(client)
    with Session(app.db.engine) as session:
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹",
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=1000,
                share=500,
                nav=2.0,
                fee=0,
            )
        )
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹",
                trade_date=date(2024, 1, 3),
                action=TransactionAction.sell,
                amount_cny=1100,
                share=500,
                nav=2.2,
                fee=0,
            )
        )
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 2), unit_nav=2.0))
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 3), unit_nav=2.2))
        session.add(
            BenchmarkNav(
                benchmark_code="000300",
                benchmark_name="沪深300",
                nav_date=date(2024, 1, 2),
                close_value=3300,
            )
        )
        session.add(
            BenchmarkNav(
                benchmark_code="000300",
                benchmark_name="沪深300",
                nav_date=date(2024, 1, 3),
                close_value=3366,
            )
        )
        session.commit()

    page = await client.get("/holdings/005827")
    assert page.status_code == 200
    assert "易方达蓝筹" in page.text
    assert "buy-marker" in page.text
    assert "sell-marker" in page.text
    assert "已实现收益" in page.text
    assert "¥100.00" in page.text


async def test_backup_export(client):
    await login(client)
    import app.db
    from app.models import BenchmarkNav

    await client.post(
        "/upload",
        data={"raw_text": "2024-01-02 005827 易方达蓝筹 buy 500 250 2.0000 0"},
    )
    await wait_for_text(client, "/candidates", "005827")
    await client.post("/candidates/1/confirm", follow_redirects=False)
    with Session(app.db.engine) as session:
        session.add(
            BenchmarkNav(
                benchmark_code="000300",
                benchmark_name="沪深300",
                nav_date=date(2024, 1, 2),
                close_value=3300,
            )
        )
        session.commit()
    response = await client.get("/backup/export")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    data = response.json()
    assert data["version"] == 1
    assert data["transactions"][0]["fund_code"] == "005827"
    assert data["imports"][0]["raw_text"]
    assert "fund_rules" in data
    assert "fund_fee_tiers" in data
    assert data["benchmark_nav"][0]["benchmark_code"] == "000300"


async def test_aliases_repair_analytics_backup_and_audit_pages(client, tmp_path, monkeypatch):
    await login(client)
    import app.db
    import app.main
    from app.app_settings import runtime_settings, save_settings
    from app.models import CandidateStatus, FundAlias, FundNav, FundRule, FundTransactionCandidate, OperationAudit, TransactionAction

    with Session(app.db.engine) as session:
        session.add(FundRule(fund_code="006327", fund_name="易方达中证海外互联网50ETF联接(QDII)A", fund_type="QDII", buy_confirm_days=2, sell_confirm_days=2))
        session.add(FundNav(fund_code="006327", nav_date=date(2025, 7, 18), unit_nav=1.1022))
        session.add(FundNav(fund_code="006327", nav_date=date(2025, 7, 21), unit_nav=1.1143))
        session.add(FundNav(fund_code="006327", nav_date=date(2025, 7, 22), unit_nav=1.13))
        session.add(
            FundTransactionCandidate(
                status=CandidateStatus.pending,
                fund_code="006327",
                fund_name="易方达中证海外互联网5OETF联接(QDI)A",
                trade_date=date(2025, 7, 18),
                action=TransactionAction.buy,
                amount_cny=50,
            )
        )
        save_settings(
            session,
            {
                "AUTO_BACKUP_ENABLED": "true",
                "AUTO_BACKUP_TIME": "02:10",
                "AUTO_BACKUP_TIMEZONE": "Asia/Shanghai",
                "AUTO_BACKUP_KEEP": "2",
                "AUTO_BACKUP_LAST_RUN_DATE": "",
            },
        )

    aliases = await client.get("/aliases")
    assert "5OETF" in aliases.text
    response = await client.post(
        "/aliases",
        data={"pattern": "中国互联网", "replacement": "互联网", "notes": "测试"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(app.db.engine) as session:
        assert session.exec(select(FundAlias)).first().pattern == "中国互联网"

    repair = await client.get("/repair")
    assert "候选可回填" in repair.text
    response = await client.post("/repair/run", follow_redirects=False)
    assert response.status_code == 303
    with Session(app.db.engine) as session:
        candidate = session.exec(select(FundTransactionCandidate)).first()
        assert candidate.nav == 1.1022
        assert candidate.share == 45.36
        assert session.exec(select(OperationAudit).where(OperationAudit.action == "repair.run")).first()

    await client.post("/candidates/1/confirm", follow_redirects=False)
    analytics = await client.get("/analytics")
    assert "收益分析" in analytics.text
    assert "累计投入" in analytics.text

    backup = await client.get("/backup")
    assert "服务器备份" in backup.text
    response = await client.post("/backup/create", follow_redirects=False)
    assert response.status_code == 303
    with Session(app.db.engine) as session:
        assert session.exec(select(OperationAudit).where(OperationAudit.action == "backup.manual_file")).first()

    now = datetime(2026, 6, 5, 2, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    job_calls = []

    class Job:
        id = 123

    def fake_create_and_enqueue(session, job_type, payload):
        job_calls.append((job_type, payload))
        return Job()

    monkeypatch.setattr(app.main, "create_and_enqueue", fake_create_and_enqueue)
    job = app.main.maybe_enqueue_auto_backup(now)
    assert job.id == 123
    assert job_calls[0][0] == "auto_backup"
    with Session(app.db.engine) as session:
        assert runtime_settings(session)["AUTO_BACKUP_LAST_RUN_DATE"] == "2026-06-05"

    audit = await client.get("/audit")
    assert "操作审计" in audit.text


async def test_health_page_reports_data_quality_issues(client):
    await login(client)
    import app.db
    from app.models import (
        BackgroundJob,
        CandidateStatus,
        FundNav,
        FundRule,
        FundTransaction,
        FundTransactionCandidate,
        ImportDocument,
        ImportStatus,
        JobStatus,
        TransactionAction,
    )

    with Session(app.db.engine) as session:
        session.add(FundRule(fund_code="161725", fund_name="招商中证白酒", buy_fee_rate=0.0))
        session.add(
            FundTransaction(
                fund_code="161725",
                fund_name="招商中证白酒",
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=1000,
                share=500,
                nav=2,
            )
        )
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 2), unit_nav=2.0))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="",
                trade_date=date(2024, 1, 3),
                action=TransactionAction.buy,
                amount_cny=500,
            )
        )
        session.add(
            FundTransactionCandidate(
                status=CandidateStatus.pending,
                fund_code="000000",
                fund_name="未知基金",
                trade_date=date(2024, 1, 4),
                action=TransactionAction.buy,
                amount_cny=100,
                confidence=0.3,
            )
        )
        session.add(ImportDocument(file_name="bad.png", status=ImportStatus.error, error_message="ocr failed"))
        session.add(BackgroundJob(job_type="sync_nav", status=JobStatus.error, error_message="offline"))
        session.commit()

    page = await client.get("/health")
    assert page.status_code == 200
    assert "数据体检" in page.text
    assert "候选交易基金未匹配" in page.text
    assert "导入文档失败" in page.text
    assert "后台任务失败" in page.text
    assert "当前持仓净值缺失或过旧" in page.text
    assert "正式流水字段不完整" in page.text
    assert "/backup" in page.text


async def test_health_page_offers_qdii_auto_review_not_hk_connect(client):
    await login(client)
    import app.db
    from app.models import FundNav, FundRule, FundTransaction, TransactionAction

    with Session(app.db.engine) as session:
        session.add(
            FundRule(
                fund_code="013308",
                fund_name="易方达恒生科技ETF联接(QDII)A",
                fund_type="指数型-海外股票",
                buy_confirm_days=1,
                sell_confirm_days=1,
                sync_source="akshare",
            )
        )
        session.add(
            FundRule(
                fund_code="021457",
                fund_name="易方达恒生港股通高股息低波动ETF联接发起式A",
                fund_type="指数型-股票",
                buy_confirm_days=1,
                sell_confirm_days=1,
                sync_source="akshare",
            )
        )
        session.add(
            FundTransaction(
                fund_code="013308",
                fund_name="易方达恒生科技ETF联接(QDII)A",
                trade_date=date(2025, 7, 18),
                action=TransactionAction.buy,
                amount_cny=50,
                share=36.39,
                nav=1.3741,
            )
        )
        session.add(
            FundTransaction(
                fund_code="021457",
                fund_name="易方达恒生港股通高股息低波动ETF联接发起式A",
                trade_date=date(2025, 7, 18),
                action=TransactionAction.buy,
                amount_cny=50,
                share=39.93,
                nav=1.2523,
            )
        )
        session.add(FundNav(fund_code="013308", nav_date=date(2026, 6, 5), unit_nav=1.1646))
        session.add(FundNav(fund_code="021457", nav_date=date(2026, 6, 5), unit_nav=1.2391))
        session.commit()

    page = await client.get("/health")
    assert "QDII/海外基金规则可自动复核" in page.text
    assert "/fund-rules/sync-qdiis" in page.text
    assert "013308" in page.text
    assert "021457" not in page.text
    rules_page = await client.get("/fund-rules")
    assert "自动复核 QDII/海外规则 1" in rules_page.text
    assert "海外规则可自动复核" in rules_page.text


async def test_qdii_auto_review_enqueues_rule_sync_jobs(client, monkeypatch):
    await login(client)
    import app.db
    import app.main
    from app.models import FundRule, FundTransaction, TransactionAction

    with Session(app.db.engine) as session:
        session.add(
            FundRule(
                fund_code="013308",
                fund_name="易方达恒生科技ETF联接(QDII)A",
                fund_type="指数型-海外股票",
                buy_confirm_days=1,
                sell_confirm_days=1,
                sync_source="akshare",
            )
        )
        session.add(
            FundRule(
                fund_code="021457",
                fund_name="易方达恒生港股通高股息低波动ETF联接发起式A",
                fund_type="指数型-股票",
                buy_confirm_days=1,
                sell_confirm_days=1,
                sync_source="akshare",
            )
        )
        session.add(
            FundTransaction(
                fund_code="013308",
                fund_name="易方达恒生科技ETF联接(QDII)A",
                trade_date=date(2025, 7, 18),
                action=TransactionAction.buy,
                amount_cny=50,
                share=36.39,
                nav=1.3741,
            )
        )
        session.add(
            FundTransaction(
                fund_code="021457",
                fund_name="易方达恒生港股通高股息低波动ETF联接发起式A",
                trade_date=date(2025, 7, 18),
                action=TransactionAction.buy,
                amount_cny=50,
                share=39.93,
                nav=1.2523,
            )
        )
        session.commit()

    calls = []

    class Job:
        id = 1

    def fake_create_and_enqueue(session, job_type, payload):
        calls.append((job_type, payload))
        return Job()

    monkeypatch.setattr(app.main, "create_and_enqueue", fake_create_and_enqueue)
    response = await client.post("/fund-rules/sync-qdiis", follow_redirects=False)
    assert response.status_code == 303
    assert calls == [("sync_fund_rule", {"fund_code": "013308"})]


async def test_ocr_import_to_candidate_flow(client, monkeypatch):
    import app.main
    from app.ocr import OcrResult

    await login(client)
    monkeypatch.setattr(
        app.main,
        "recognize_file",
        lambda *_: OcrResult(
            text="2024-01-02 161725 招商中证白酒 buy 1000 500 2.0000 0",
            confidence=0.99,
        ),
    )
    response = await client.post(
        "/upload",
        files={"file": ("trade.png", b"fake image", "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/imports/1")
    detail = await wait_for_text(client, "/imports/1", "招商中证白酒")
    assert "招商中证白酒" in detail.text

    page = await wait_for_text(client, "/candidates", "161725")
    assert "161725" in page.text
    assert "pending" in page.text


async def test_batch_upload_auto_imports_multiple_files(client, monkeypatch):
    import app.main
    from app.ocr import OcrResult

    await login(client)

    def fake_ocr(path, *_):
        text = (
            "2024-01-02 161725 招商中证白酒 buy 1000 500 2.0000 0"
            if "one" in str(path)
            else "2024-01-03 005827 易方达蓝筹 buy 500 250 2.0000 0"
        )
        return OcrResult(text=text, confidence=0.99)

    monkeypatch.setattr(app.main, "recognize_file", fake_ocr)
    response = await client.post(
        "/upload",
        files=[
            ("files", ("one.png", b"fake image 1", "image/png")),
            ("files", ("two.png", b"fake image 2", "image/png")),
        ],
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/imports?message=")

    imports = await wait_for_text(client, "/imports", "two.png")
    assert "one.png" in imports.text
    assert "two.png" in imports.text

    await wait_for_text(client, "/candidates", "005827")
    page = await wait_for_text(client, "/candidates", "161725")
    assert "161725" in page.text
    assert "005827" in page.text


async def test_import_detail_shows_audit_and_retry_reparses(client):
    import app.db
    from app.models import CandidateStatus, FundTransactionCandidate, ImportDocument, ImportStatus, TransactionAction

    await login(client)
    with Session(app.db.engine) as session:
        document = ImportDocument(
            file_name="retry.txt",
            source_hash="retry-source",
            ocr_text="2024-01-02 161725 招商中证白酒 buy 1000 500 2.0000 0",
            status=ImportStatus.error,
            error_message="old failure",
        )
        session.add(document)
        session.commit()
        session.refresh(document)
        session.add(
            FundTransactionCandidate(
                status=CandidateStatus.pending,
                fund_code="000000",
                fund_name="旧候选",
                trade_date=date(2024, 1, 1),
                action=TransactionAction.buy,
                amount_cny=1,
                source_hash=document.source_hash,
            )
        )
        session.commit()
        document_id = document.id

    detail = await client.get(f"/imports/{document_id}")
    assert "导入审计" in detail.text
    assert "待确认" in detail.text
    assert "未匹配" in detail.text

    response = await client.post(f"/imports/{document_id}/retry", follow_redirects=False)
    assert response.status_code == 303
    await wait_for_text(client, "/candidates", "161725")

    with Session(app.db.engine) as session:
        candidates = session.exec(select(FundTransactionCandidate)).all()
    assert len(candidates) == 1
    assert candidates[0].fund_code == "161725"


async def test_imports_page_shows_audit_and_bulk_retries_failed(client):
    import app.db
    from app.models import CandidateStatus, FundTransactionCandidate, ImportDocument, ImportStatus, TransactionAction

    await login(client)
    with Session(app.db.engine) as session:
        document = ImportDocument(
            file_name="failed.txt",
            source_hash="failed-source",
            ocr_text="2024-01-02 161725 招商中证白酒 buy 1000 500 2.0000 0",
            status=ImportStatus.error,
            error_message="parse failed",
        )
        session.add(document)
        session.commit()
        session.refresh(document)
        session.add(
            FundTransactionCandidate(
                status=CandidateStatus.pending,
                fund_code="000000",
                fund_name="旧候选",
                trade_date=date(2024, 1, 1),
                action=TransactionAction.buy,
                amount_cny=1,
                source_hash=document.source_hash,
            )
        )
        session.commit()

    page = await client.get("/imports")
    assert "重跑失败 1" in page.text
    assert "候选 1" in page.text
    assert "未匹配 1" in page.text

    response = await client.post("/imports/retry-failed", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/imports?message=")
    await wait_for_text(client, "/candidates", "161725")

    with Session(app.db.engine) as session:
        candidates = session.exec(select(FundTransactionCandidate)).all()
    assert len(candidates) == 1
    assert candidates[0].fund_code == "161725"


async def test_auto_import_similarity_message_does_not_crash(client, tmp_path, monkeypatch):
    import app.db
    import app.main
    from app.models import ImportDocument, ImportStatus
    from app.ocr import OcrResult

    repeated_text = "2024-01-02 161725 招商中证白酒 buy 1000 500 2.0000 0"
    upload_path = tmp_path / "new.png"
    upload_path.write_bytes(b"fake image")
    monkeypatch.setattr(app.main, "recognize_file", lambda *_: OcrResult(text=repeated_text, confidence=0.99))
    with Session(app.db.engine) as session:
        old_doc = ImportDocument(
            file_name="old.png",
            ocr_text=repeated_text,
            source_hash="old",
            status=ImportStatus.parse_done,
        )
        new_doc = ImportDocument(
            file_name="new.png",
            source_file=str(upload_path),
            source_hash="new",
            status=ImportStatus.uploaded,
        )
        session.add(old_doc)
        session.add(new_doc)
        session.commit()
        session.refresh(old_doc)
        session.refresh(new_doc)
        old_id = old_doc.id
        new_id = new_doc.id

    message = app.main.process_auto_import_job({"document_id": new_id})
    assert f"发现相似文档 #{old_id}" in message
    with Session(app.db.engine) as session:
        updated = session.get(ImportDocument, new_id)
        assert f"导入 #{old_id}" in updated.error_message


async def test_queued_jobs_are_reenqueued_on_recovery(client):
    import app.db
    from app.jobs import recover_interrupted_jobs, register_job
    from app.models import BackgroundJob, JobStatus

    register_job("resume_test", lambda payload: "resumed")
    with Session(app.db.engine) as session:
        job = BackgroundJob(job_type="resume_test", payload_json="{}")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    for _ in range(20):
        with Session(app.db.engine) as session:
            current = session.get(BackgroundJob, job_id)
            if current.status == JobStatus.done:
                break
        await asyncio.sleep(0.05)
    with Session(app.db.engine) as session:
        current = session.get(BackgroundJob, job_id)
        assert current.status == JobStatus.queued

    recover_interrupted_jobs()
    for _ in range(20):
        with Session(app.db.engine) as session:
            current = session.get(BackgroundJob, job_id)
            if current.status == JobStatus.done:
                break
        await asyncio.sleep(0.05)
    with Session(app.db.engine) as session:
        current = session.get(BackgroundJob, job_id)
        assert current.status == JobStatus.done
        assert current.result_message == "resumed"


async def test_settings_page_saves_runtime_config(client):
    await login(client)
    response = await client.post(
        "/settings",
        data={
            "deepseek_enabled": "on",
            "deepseek_api_key": "sk-test-secret",
            "deepseek_base_url": "https://api.deepseek.example",
            "deepseek_model": "deepseek-chat",
            "ocr_enabled": "on",
            "ocr_backend": "api",
            "ocr_api_provider": "generic",
            "ocr_api_url": "https://ocr.example/parse",
            "ocr_api_auth_header": "X-API-Key",
            "ocr_api_auth_prefix": "",
            "ocr_api_key": "ocr-secret",
            "ocr_api_file_field": "image",
            "ocr_api_text_path": "data.text",
            "baidu_ocr_api_key": "",
            "baidu_ocr_secret_key": "",
            "baidu_ocr_endpoint": "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic",
            "auto_market_sync_enabled": "on",
            "auto_market_sync_time": "21:30",
            "auto_market_sync_timezone": "Asia/Shanghai",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = await client.get("/settings")
    assert "api.deepseek.example" in page.text
    assert "https://ocr.example/parse" in page.text
    assert "sk-test-secret" not in page.text
    assert "ocr-secret" not in page.text
    assert "末尾 cret" in page.text
    assert "已启用且已配置" in page.text
    assert "21:30" in page.text
    assert "Asia/Shanghai" in page.text


async def test_daily_market_sync_scheduler_enqueues_once(client, monkeypatch):
    import app.db
    import app.main
    from app.app_settings import runtime_settings, save_settings
    from app.models import BackgroundJob
    from datetime import datetime
    from zoneinfo import ZoneInfo

    await login(client)
    calls = []

    def fake_create_and_enqueue(session, job_type, payload):
        calls.append((job_type, payload))
        return BackgroundJob(id=99, job_type=job_type)

    monkeypatch.setattr(app.main, "create_and_enqueue", fake_create_and_enqueue)
    with Session(app.db.engine) as session:
        save_settings(
            session,
            {
                "AUTO_MARKET_SYNC_ENABLED": "true",
                "AUTO_MARKET_SYNC_TIME": "21:30",
                "AUTO_MARKET_SYNC_TIMEZONE": "Asia/Shanghai",
                "AUTO_MARKET_SYNC_LAST_RUN_DATE": "",
            },
        )

    now = datetime(2026, 6, 5, 21, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    job = app.main.maybe_enqueue_daily_market_sync(now)
    assert job.id == 99
    assert calls == [("daily_market_sync", {"date": "2026-06-05", "timezone": "Asia/Shanghai", "scheduled_time": "21:30"})]
    with Session(app.db.engine) as session:
        assert runtime_settings(session)["AUTO_MARKET_SYNC_LAST_RUN_DATE"] == "2026-06-05"
    assert app.main.maybe_enqueue_daily_market_sync(now) is None


async def test_nav_sync_current_creates_daily_job(client):
    await login(client)
    response = await client.post("/nav/sync-current", follow_redirects=False)
    assert response.status_code == 303
    page = await wait_for_text(client, "/nav", "daily_market_sync")
    assert "当前持仓和曲线同步" in page.text or "daily_market_sync" in page.text


async def test_import_archive_delete_restore(client):
    await login(client)
    await client.post(
        "/upload",
        data={"raw_text": "2024-01-02 005827 易方达蓝筹 buy 500 250 2.0000 0"},
        follow_redirects=False,
    )
    await wait_for_text(client, "/imports/1", "parse_done")
    response = await client.post("/imports/1/archive", follow_redirects=False)
    assert response.status_code == 303
    page = await client.get("/imports")
    assert "archived" in page.text

    response = await client.post("/imports/1/restore", follow_redirects=False)
    assert response.status_code == 303
    detail = await client.get("/imports/1")
    assert "uploaded" in detail.text

    response = await client.post("/imports/1/delete", follow_redirects=False)
    assert response.status_code == 303
    page = await client.get("/imports")
    assert "易方达蓝筹" not in page.text
    page = await client.get("/imports?show=all")
    assert "deleted" in page.text


async def test_minimal_buy_infers_nav_after_cutoff(client):
    import app.db
    from app.models import FundNav

    await login(client)
    with Session(app.db.engine) as session:
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 3), unit_nav=2.0))
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 4), unit_nav=2.1))
        session.commit()

    await client.post(
        "/upload",
        data={"raw_text": "2024-01-02 15:30 161725 招商中证白酒 买入 1000元"},
        follow_redirects=False,
    )
    page = await wait_for_text(client, "/candidates", "161725")
    assert "161725" in page.text
    assert "15:30" in page.text
    assert "2024-01-03" in page.text
    assert "500.0" in page.text
    assert "2.0" in page.text


async def test_candidate_confirm_backfills_missing_qdii_values_from_nav(client):
    import app.db
    from app.models import CandidateStatus, FundNav, FundRule, FundTransaction, FundTransactionCandidate, TransactionAction

    await login(client)
    with Session(app.db.engine) as session:
        session.add(
            FundRule(
                fund_code="006327",
                fund_name="易方达中证海外互联网50ETF联接(QDII)A",
                fund_type="QDII",
                buy_confirm_days=2,
                sell_confirm_days=2,
                buy_fee_rate=0.0,
            )
        )
        session.add(FundNav(fund_code="006327", nav_date=date(2025, 7, 18), unit_nav=1.1022))
        session.add(FundNav(fund_code="006327", nav_date=date(2025, 7, 21), unit_nav=1.1143))
        session.add(FundNav(fund_code="006327", nav_date=date(2025, 7, 22), unit_nav=1.13))
        session.add(
            FundTransactionCandidate(
                status=CandidateStatus.pending,
                fund_code="006327",
                fund_name="易方达中证海外互联网50ETF联接(QDII)A",
                trade_date=date(2025, 7, 18),
                action=TransactionAction.buy,
                amount_cny=50,
                confidence=0.85,
            )
        )
        session.commit()

    response = await client.post("/candidates/1/confirm", follow_redirects=False)
    assert response.status_code == 303
    with Session(app.db.engine) as session:
        tx = session.exec(select(FundTransaction)).one()
    assert tx.nav == 1.1022
    assert tx.share == 45.36
    assert tx.confirm_date == date(2025, 7, 22)


async def test_manual_transaction_without_nav_persists_resolved_nav(client):
    import app.db
    from app.models import FundNav, FundRule

    await login(client)
    with Session(app.db.engine) as session:
        session.add(FundRule(fund_code="161725", fund_name="招商中证白酒", buy_fee_rate=0.0))
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 2), unit_nav=2.0))
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 3), unit_nav=2.1))
        session.commit()

    response = await client.post(
        "/transactions",
        data={
            "fund_code": "161725",
            "trade_date": "2024-01-02",
            "action": "buy",
            "amount_cny": "1000",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = await client.get("/transactions")
    assert "2.0000" in page.text
    assert "500.0000" in page.text


async def test_llm_rows_apply_submitted_time_cutoff(client):
    import app.db
    from app.main import create_candidates_from_rows
    from app.models import FundNav, FundTransactionCandidate

    await login(client)
    with Session(app.db.engine) as session:
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 2), unit_nav=1.9))
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 3), unit_nav=2.0))
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 4), unit_nav=2.1))
        create_candidates_from_rows(
            session,
            [
                {
                    "fund_code": "161725",
                    "fund_name": "招商中证白酒",
                    "trade_date": "2024-01-02",
                    "submitted_at": "15:30",
                    "action": "buy",
                    "amount_cny": 1000,
                }
            ],
        )
        session.commit()
        candidate = session.exec(select(FundTransactionCandidate)).first()

    assert candidate.submitted_at.strftime("%H:%M") == "15:30"
    assert candidate.trade_date == date(2024, 1, 3)
    assert candidate.confirm_date == date(2024, 1, 4)
    assert candidate.nav == 2.0


async def test_money_fund_uses_cash_equivalent_values(client):
    import app.db
    from app.main import create_candidates_from_rows, normalize_money_fund_records
    from app.models import FundRule, FundTransaction, FundTransactionCandidate, TransactionAction
    from app.portfolio import calculate_holdings

    await login(client)
    with Session(app.db.engine) as session:
        session.add(
            FundRule(
                fund_code="000621",
                fund_name="易方达现金增利货币B",
                fund_type="货币型-普通货币",
            )
        )
        create_candidates_from_rows(
            session,
            [
                {
                    "fund_code": "000621",
                    "fund_name": "易方达现金增利货币B",
                    "trade_date": "2025-04-23",
                    "action": "buy",
                    "amount_cny": 700,
                    "nav": 0.4436,
                },
                {
                    "fund_code": "000621",
                    "fund_name": "易方达现金增利货币B",
                    "trade_date": "2025-04-24",
                    "action": "sell",
                    "share": 100,
                    "nav": 0.4436,
                },
            ],
        )
        session.commit()
        candidates = session.exec(select(FundTransactionCandidate).order_by(FundTransactionCandidate.id)).all()
        assert [(c.amount_cny, c.share, c.nav, c.fee) for c in candidates] == [
            (700, 700, 1.0, 0.0),
            (100, 100, 1.0, 0.0),
        ]
        session.add(
            FundTransaction(
                fund_code="000621",
                fund_name="易方达现金增利货币B",
                trade_date=date(2025, 4, 23),
                action=TransactionAction.buy,
                amount_cny=700,
                share=1578,
                nav=0.4436,
                fee=0,
            )
        )
        session.commit()

    normalize_money_fund_records()
    with Session(app.db.engine) as session:
        tx = session.exec(select(FundTransaction)).first()
        assert (tx.amount_cny, tx.share, tx.nav, tx.fee) == (700, 700, 1.0, 0.0)
        holding = calculate_holdings(session)[0]
        assert holding.latest_nav == 1.0
        assert holding.market_value == 700


async def test_same_day_money_buy_is_applied_before_sell_for_cost(client):
    import app.db
    from app.models import FundRule, FundTransaction, TransactionAction
    from app.portfolio import calculate_position_summaries

    await login(client)
    with Session(app.db.engine) as session:
        session.add(
            FundRule(
                fund_code="000621",
                fund_name="易方达现金增利货币B",
                fund_type="货币型-普通货币",
            )
        )
        session.add(
            FundTransaction(
                fund_code="000621",
                fund_name="易方达现金增利货币B",
                trade_date=date(2025, 4, 23),
                action=TransactionAction.sell,
                amount_cny=700,
                share=700,
                nav=1.0,
                fee=0,
            )
        )
        session.add(
            FundTransaction(
                fund_code="000621",
                fund_name="易方达现金增利货币B",
                trade_date=date(2025, 4, 23),
                action=TransactionAction.buy,
                amount_cny=700,
                share=700,
                nav=1.0,
                fee=0,
            )
        )
        session.commit()
        position = calculate_position_summaries(session)[0]

    assert position.share == 0
    assert position.cost == 0
    assert position.realized_profit == 0
    assert position.is_closed


async def test_candidate_update_preserves_existing_effective_trade_date(client):
    import app.db
    from app.main import create_candidates_from_rows
    from app.models import FundNav, FundTransactionCandidate

    await login(client)
    with Session(app.db.engine) as session:
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 2), unit_nav=1.9))
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 3), unit_nav=2.0))
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 4), unit_nav=2.1))
        create_candidates_from_rows(
            session,
            [
                {
                    "fund_code": "161725",
                    "fund_name": "招商中证白酒",
                    "trade_date": "2024-01-02",
                    "submitted_at": "15:30",
                    "action": "buy",
                    "amount_cny": 1000,
                }
            ],
        )
        session.commit()
        candidate = session.exec(select(FundTransactionCandidate)).first()
        candidate_id = candidate.id
        assert candidate.trade_date == date(2024, 1, 3)
        assert candidate.confirm_date == date(2024, 1, 4)

    response = await client.post(
        f"/candidates/{candidate_id}/update",
        data={
            "fund_code": "161725",
            "fund_name": "招商中证白酒",
            "trade_date": "2024-01-03",
            "submitted_at": "15:30",
            "confirm_date": "2024-01-04",
            "action": "buy",
            "amount_cny": "1000",
            "share": "500",
            "nav": "2.0",
            "fee": "0",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(app.db.engine) as session:
        candidate = session.get(FundTransactionCandidate, candidate_id)
        assert candidate.trade_date == date(2024, 1, 3)
        assert candidate.confirm_date == date(2024, 1, 4)


async def test_fund_rule_controls_t_plus_confirm_date(client):
    import app.db
    from app.models import FundNav

    await login(client)
    response = await client.post(
        "/fund-rules",
        data={
            "fund_code": "161725",
            "fund_name": "招商中证白酒",
            "buy_confirm_days": "2",
            "sell_confirm_days": "1",
            "cutoff_time": "15:00",
            "buy_fee_rate": "0",
            "notes": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(app.db.engine) as session:
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 2), unit_nav=2.0))
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 3), unit_nav=2.1))
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 4), unit_nav=2.2))
        session.commit()

    await client.post(
        "/upload",
        data={"raw_text": "2024-01-02 14:30 161725 招商中证白酒 买入 1000元"},
        follow_redirects=False,
    )
    page = await wait_for_text(client, "/candidates", "2024-01-04")
    assert "2024-01-02" in page.text
    assert "2024-01-04" in page.text


async def test_sell_fee_uses_fifo_fee_tiers(client):
    import app.db
    from app.models import FundNav

    await login(client)
    await client.post(
        "/fund-rules",
        data={
            "fund_code": "161725",
            "fund_name": "招商中证白酒",
            "buy_confirm_days": "1",
            "sell_confirm_days": "1",
            "cutoff_time": "15:00",
            "buy_fee_rate": "0",
            "notes": "",
        },
        follow_redirects=False,
    )
    await client.post(
        "/fund-rules/161725/tiers",
        data={"min_holding_days": "0", "max_holding_days": "7", "redemption_fee_rate": "0.015"},
        follow_redirects=False,
    )
    await client.post(
        "/fund-rules/161725/tiers",
        data={"min_holding_days": "7", "max_holding_days": "", "redemption_fee_rate": "0"},
        follow_redirects=False,
    )
    with Session(app.db.engine) as session:
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 2), unit_nav=2.0))
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 3), unit_nav=2.0))
        session.commit()

    await client.post(
        "/upload",
        data={"raw_text": "2024-01-02 10:00 161725 招商中证白酒 买入 1000元"},
        follow_redirects=False,
    )
    await wait_for_text(client, "/candidates", "161725")
    await client.post("/candidates/1/confirm", follow_redirects=False)
    await client.post(
        "/upload",
        data={"raw_text": "2024-01-03 10:00 161725 招商中证白酒 赎回 100份"},
        follow_redirects=False,
    )
    page = await wait_for_text(client, "/candidates", "3.0")
    assert "3.0" in page.text


async def test_rule_parser_handles_confirm_and_fee_text(monkeypatch):
    import pandas as pd
    import app.fund_rule_sync as sync
    from app.fund_rule_sync import parse_confirm_days, parse_redemption_fee_tiers, search_fund_by_name

    confirm_df = [
        {"费用类型": "交易确认日", "条件或名称": "买入确认 T+1"},
        {"费用类型": "交易确认日", "条件或名称": "卖出确认 T+2"},
    ]
    assert parse_confirm_days(confirm_df) == (1, 2)

    tiers = parse_redemption_fee_tiers(
        [
            {"费用类型": "赎回费率", "条件或名称": "小于7天", "费用": "1.50%"},
            {"费用类型": "赎回费率", "条件或名称": "大于等于7天，小于365天", "费用": "0.50%"},
            {"费用类型": "赎回费率", "条件或名称": "大于等于365天", "费用": "0.00%"},
        ]
    )
    assert tiers == [(0, 7, 0.015), (7, 365, 0.005), (365, None, 0.0)]
    monkeypatch.setattr(
        sync,
        "_fund_list_cache",
        pd.DataFrame(
            [
                {
                    "基金代码": "025937",
                    "基金简称": "华泰柏瑞恒生港股通高股息低波动ETF发起式联接A",
                    "基金类型": "指数型-股票",
                },
                {
                    "基金代码": "025938",
                    "基金简称": "华泰柏瑞恒生港股通高股息低波动ETF发起式联接C",
                    "基金类型": "指数型-股票",
                },
            ]
        ),
    )
    monkeypatch.setattr(sync, "_fund_list_failed", False)
    monkeypatch.setattr(sync, "search_fund_by_name_sina", lambda _: None)
    monkeypatch.setattr(sync, "search_fund_by_name_eastmoney", lambda _: None)
    assert search_fund_by_name("00") is None
    assert search_fund_by_name("25") is None
    result = search_fund_by_name("易方达恒生港股通高股息低波动ETF联接发起式A")
    assert result is None
    result = search_fund_by_name("华泰柏瑞恒生港股通高股息低波动ETF联接发起式A")
    assert result["fund_code"] == "025937"


async def test_fund_name_search_normalizes_ocr_digit_letter_noise(monkeypatch):
    import pandas as pd
    import app.fund_rule_sync as sync
    from app.fund_rule_sync import search_fund_by_name

    monkeypatch.setattr(
        sync,
        "_fund_list_cache",
        pd.DataFrame(
            [
                {
                    "基金代码": "006327",
                    "基金简称": "易方达中证海外互联网50ETF联接(QDII)A",
                    "基金类型": "QDII",
                }
            ]
        ),
    )
    monkeypatch.setattr(sync, "_fund_list_failed", False)
    monkeypatch.setattr(sync, "search_fund_by_name_sina", lambda _: None)
    monkeypatch.setattr(sync, "search_fund_by_name_eastmoney", lambda _: None)
    result = search_fund_by_name("易方达中证海外中国互联网5OETF联接（QDII）A(人民币份额)")
    assert result["fund_code"] == "006327"


async def test_known_fund_name_match_normalizes_ocr_noise():
    from app.main import find_known_fund_code

    known_names = {"易方达中证海外互联网50ETF联接(QDII)A": "006327"}
    assert (
        find_known_fund_code(
            "易方达中证海外中国互联网5OETF联接（QDII）A(人民币份额)",
            known_names,
        )
        == "006327"
    )


async def test_fund_name_search_uses_full_sina_suggestion(monkeypatch):
    import app.fund_rule_sync as sync
    from app.fund_rule_sync import search_fund_by_name_sina

    class FakeResponse:
        encoding = "utf-8"
        text = (
            'var suggestvalue="易方达恒生港股通高股息低波动ETF联接发起式A,201,021457,'
            "of021457,易方达恒生港股通高股息低波动ETF联接发起式A,,"
            "易方达恒生港股通高股息低波动ETF联接发起式A,99,1,,,;"
            "易方达恒生港股通高股息低波动ETF联接发起式C,201,021458,"
            "of021458,易方达恒生港股通高股息低波动ETF联接发起式C,,"
            '易方达恒生港股通高股息低波动ETF联接发起式C,99,1,,,";'
        )

        def raise_for_status(self):
            return None

    class FakeRequests:
        @staticmethod
        def get(*_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setitem(__import__("sys").modules, "requests", FakeRequests)
    result = search_fund_by_name_sina("易方达恒生港股通高股息低波动ETF联接发起式A")
    assert result == {
        "fund_code": "021457",
        "fund_name": "易方达恒生港股通高股息低波动ETF联接发起式A",
        "fund_type": "场外基金",
    }


async def test_dividend_and_reinvest_calculation(client):
    import app.db
    from app.main import create_candidates_from_rows
    from app.models import FundNav, FundTransaction, FundTransactionCandidate, TransactionAction
    from app.portfolio import calculate_position_summaries

    await login(client)
    with Session(app.db.engine) as session:
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 2), unit_nav=2.0))
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 3), unit_nav=2.0))
        create_candidates_from_rows(
            session,
            [
                {
                    "fund_code": "161725",
                    "fund_name": "招商中证白酒",
                    "trade_date": "2024-01-02",
                    "action": "dividend_reinvest",
                    "amount_cny": 20,
                },
                {
                    "fund_code": "161725",
                    "fund_name": "招商中证白酒",
                    "trade_date": "2024-01-02",
                    "action": "dividend",
                    "amount_cny": 5,
                    "share": 99,
                },
            ],
        )
        session.commit()
        candidates = session.exec(select(FundTransactionCandidate).order_by(FundTransactionCandidate.id)).all()
        assert (candidates[0].amount_cny, candidates[0].share, candidates[0].fee) == (20, 10, 0.0)
        assert (candidates[1].amount_cny, candidates[1].share, candidates[1].fee) == (5, None, 0.0)
        session.add(
            FundTransaction(
                fund_code="161725",
                fund_name="招商中证白酒",
                trade_date=date(2024, 1, 1),
                action=TransactionAction.buy,
                amount_cny=100,
                share=50,
                nav=2.0,
                fee=0,
            )
        )
        session.add(
            FundTransaction(
                fund_code="161725",
                fund_name="招商中证白酒",
                trade_date=date(2024, 1, 2),
                action=TransactionAction.dividend,
                amount_cny=5,
                fee=0,
            )
        )
        session.add(
            FundTransaction(
                fund_code="161725",
                fund_name="招商中证白酒",
                trade_date=date(2024, 1, 2),
                action=TransactionAction.dividend_reinvest,
                amount_cny=20,
                share=10,
                nav=2.0,
                fee=0,
            )
        )
        session.commit()
        position = calculate_position_summaries(session)[0]
    assert position.share == 60
    assert position.cost == 95
    assert position.realized_profit == 5


async def test_fund_rule_auto_sync_creates_rule_and_tiers(client, monkeypatch):
    import app.main
    from app.fund_rule_sync import SyncedRule

    await login(client)
    monkeypatch.setattr(
        app.main,
        "fetch_fund_rule_from_akshare",
        lambda code: SyncedRule(
            fund_code=code,
            fund_name="招商中证白酒",
            buy_confirm_days=1,
            sell_confirm_days=2,
            buy_fee_rate=0.0,
            fee_tiers=[(0, 7, 0.015), (7, None, 0.0)],
            source="akshare",
        ),
    )
    response = await client.post("/fund-rules/sync", data={"fund_code": "161725"}, follow_redirects=False)
    assert response.status_code == 303
    page = await wait_for_text(client, "/fund-rules", "卖出 T+2")
    assert "招商中证白酒" in page.text
    assert "买入 T+1" in page.text
    assert "卖出 T+2" in page.text
    assert "0.0150" in page.text
    assert "akshare" in page.text


async def test_fund_rule_sync_failure_keeps_existing_rule(client, monkeypatch):
    import app.main

    await login(client)
    await client.post(
        "/fund-rules",
        data={
            "fund_code": "161725",
            "fund_name": "人工规则",
            "buy_confirm_days": "2",
            "sell_confirm_days": "2",
            "cutoff_time": "15:00",
            "buy_fee_rate": "0",
            "notes": "",
        },
        follow_redirects=False,
    )
    monkeypatch.setattr(
        app.main,
        "fetch_fund_rule_from_akshare",
        lambda code: (_ for _ in ()).throw(RuntimeError("source down")),
    )
    response = await client.post("/fund-rules/sync", data={"fund_code": "161725"}, follow_redirects=False)
    assert response.status_code == 303
    page = await client.get("/fund-rules")
    assert "人工规则" in page.text
    assert "买入 T+2" in page.text


async def test_fix_unmatched_keeps_candidate_update_when_rule_sync_fails(client, monkeypatch):
    import app.db
    import app.main
    from app.models import CandidateStatus, FundTransactionCandidate, TransactionAction

    await login(client)
    monkeypatch.setattr(
        app.main,
        "fetch_fund_rule_from_akshare",
        lambda code: (_ for _ in ()).throw(RuntimeError("source down")),
    )
    monkeypatch.setattr(app.main, "sync_nav_for_fund", lambda *_: (0, None))
    with Session(app.db.engine) as session:
        candidate = FundTransactionCandidate(
            status=CandidateStatus.pending,
            fund_code="000000",
            fund_name="待修正基金",
            trade_date=date(2024, 1, 2),
            action=TransactionAction.buy,
            amount_cny=100,
        )
        session.add(candidate)
        session.commit()
        session.refresh(candidate)
        candidate_id = candidate.id

    response = await client.post(
        "/candidates/fix-unmatched",
        data={"fund_name": "待修正基金", "fund_code": "161725"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    from urllib.parse import unquote

    assert "规则同步失败" in unquote(response.headers["location"])
    with Session(app.db.engine) as session:
        candidate = session.get(FundTransactionCandidate, candidate_id)
        assert candidate.fund_code == "161725"
        assert candidate.confidence == 0.8


async def test_sync_nav_uses_pz_and_upserts_existing_dates(client, monkeypatch):
    import sys
    import types
    import pandas as pd
    import app.db
    from app.models import FundNav
    from app.nav import sync_nav_for_fund

    calls = []

    class FakeFund:
        @staticmethod
        def get_quote_history(fund_code, pz=40000):
            calls.append((fund_code, pz))
            return pd.DataFrame(
                [
                    {"日期": "2024-01-02", "单位净值": "2.1000", "累计净值": "2.1000", "涨跌幅": "1.00"},
                    {"日期": "2024-01-03", "单位净值": "2.2000", "累计净值": "2.2000", "涨跌幅": "2.00"},
                ]
            )

    monkeypatch.setitem(sys.modules, "efinance", types.SimpleNamespace(fund=FakeFund))
    with Session(app.db.engine) as session:
        session.add(FundNav(fund_code="161725", nav_date=date(2024, 1, 2), unit_nav=2.0))
        session.commit()
        inserted, error = sync_nav_for_fund(session, "161725", pz=90)
        navs = session.exec(select(FundNav).order_by(FundNav.nav_date)).all()

    assert error is None
    assert inserted == 1
    assert calls == [("161725", 90)]
    assert [(item.nav_date, item.unit_nav) for item in navs] == [
        (date(2024, 1, 2), 2.1),
        (date(2024, 1, 3), 2.2),
    ]


async def test_daily_market_sync_uses_fast_nav_without_rule_sync(client, monkeypatch):
    import app.main
    import app.db
    from app.models import FundTransaction, TransactionAction

    await login(client)
    calls = []
    monkeypatch.setattr(
        app.main,
        "fetch_fund_rule_from_akshare",
        lambda code: (_ for _ in ()).throw(RuntimeError("rule sync should be skipped")),
    )
    monkeypatch.setattr(app.main, "sync_nav_for_fund", lambda session, code, pz=40000: calls.append((code, pz)) or (1, None))
    monkeypatch.setattr(app.main, "sync_hs300", lambda session: (1, None))
    with Session(app.db.engine) as session:
        session.add(
            FundTransaction(
                fund_code="161725",
                fund_name="招商中证白酒",
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=50,
                nav=2,
            )
        )
        session.commit()

    message = app.main.process_daily_market_sync_job({})
    assert "每日同步完成" in message
    assert calls == [("161725", 90)]


async def test_failed_minimal_order_is_ignored(client):
    await login(client)
    await client.post(
        "/upload",
        data={"raw_text": "2024-01-02 10:00 161725 招商中证白酒 买入 1000元 交易失败"},
        follow_redirects=False,
    )
    page = await wait_for_text(client, "/candidates", "ignored")
    assert "ignored" in page.text

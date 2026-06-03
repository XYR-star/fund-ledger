from datetime import date
import asyncio

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


async def test_rule_parser_handles_confirm_and_fee_text():
    from app.fund_rule_sync import parse_confirm_days, parse_redemption_fee_tiers

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


async def test_failed_minimal_order_is_ignored(client):
    await login(client)
    await client.post(
        "/upload",
        data={"raw_text": "2024-01-02 10:00 161725 招商中证白酒 买入 1000元 交易失败"},
        follow_redirects=False,
    )
    page = await wait_for_text(client, "/candidates", "ignored")
    assert "ignored" in page.text

from datetime import date
import asyncio

import httpx
import pytest
from sqlmodel import Session, SQLModel


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

    importlib.reload(app.config)
    importlib.reload(app.db)
    importlib.reload(app.main)
    monkeypatch.setattr(app.main, "sync_nav_for_fund", lambda *_: (0, "offline"))
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


async def wait_for_text(client, path, text, attempts=20):
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

    page = await client.get("/candidates")
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
    await client.post("/candidates/1/ignore", follow_redirects=False)
    tx_page = await client.get("/transactions")
    assert "易方达蓝筹" not in tx_page.text


async def test_holdings_calculation_without_synced_nav(client):
    await login(client)
    await client.post(
        "/upload",
        data={"raw_text": "2024-01-02 005827 易方达蓝筹 buy 500 250 2.0000 0"},
    )
    await client.post("/candidates/1/confirm", follow_redirects=False)
    page = await client.get("/holdings")
    assert "005827" in page.text
    assert "250.0000" in page.text
    assert "¥500.00" in page.text


async def test_backup_export(client):
    await login(client)
    await client.post(
        "/upload",
        data={"raw_text": "2024-01-02 005827 易方达蓝筹 buy 500 250 2.0000 0"},
    )
    await client.post("/candidates/1/confirm", follow_redirects=False)
    response = await client.get("/backup/export")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    data = response.json()
    assert data["version"] == 1
    assert data["transactions"][0]["fund_code"] == "005827"
    assert data["imports"][0]["raw_text"]
    assert "fund_rules" in data
    assert "fund_fee_tiers" in data


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
    assert response.headers["location"] == "/imports/1"

    response = await client.post("/imports/1/ocr", follow_redirects=False)
    assert response.status_code == 303
    detail = await wait_for_text(client, "/imports/1", "招商中证白酒")
    assert "招商中证白酒" in detail.text

    response = await client.post("/imports/1/parse", follow_redirects=False)
    assert response.status_code == 303
    page = await wait_for_text(client, "/candidates", "161725")
    assert "161725" in page.text
    assert "pending" in page.text


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
    page = await client.get("/candidates")
    assert "161725" in page.text
    assert "2024-01-03" in page.text
    assert "500.0" in page.text
    assert "2.0" in page.text


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
    page = await client.get("/candidates")
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
    await client.post("/candidates/1/confirm", follow_redirects=False)
    await client.post(
        "/upload",
        data={"raw_text": "2024-01-03 10:00 161725 招商中证白酒 赎回 100份"},
        follow_redirects=False,
    )
    page = await client.get("/candidates")
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
    page = await client.get("/candidates")
    assert "ignored" in page.text

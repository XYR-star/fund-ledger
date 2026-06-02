import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel


@pytest.fixture()
def client(tmp_path, monkeypatch):
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
    SQLModel.metadata.drop_all(app.db.engine)
    SQLModel.metadata.create_all(app.db.engine)
    with TestClient(app.main.app) as test_client:
        yield test_client


def login(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "changeme", "next": "/"},
        follow_redirects=False,
    )


def test_requires_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_login_success(client):
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_candidate_confirm_flow(client):
    login(client)
    response = client.post(
        "/upload",
        data={
            "raw_text": "2024-01-02 161725 招商中证白酒 buy 1000 - 1.0000 1.00"
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    page = client.get("/candidates")
    assert "161725" in page.text
    assert "pending" in page.text

    response = client.post("/candidates/1/confirm", follow_redirects=False)
    assert response.status_code == 303
    tx_page = client.get("/transactions")
    assert "招商中证白酒" in tx_page.text
    assert "¥1000.00" in tx_page.text

    response = client.post("/candidates/1/confirm", follow_redirects=False)
    assert response.status_code == 303
    tx_page = client.get("/transactions")
    assert tx_page.text.count("招商中证白酒") == 1


def test_ignore_candidate_does_not_create_transaction(client):
    login(client)
    client.post(
        "/upload",
        data={"raw_text": "2024-01-02 005827 易方达蓝筹 buy 500 - 2.0000 0"},
    )
    client.post("/candidates/1/ignore", follow_redirects=False)
    tx_page = client.get("/transactions")
    assert "易方达蓝筹" not in tx_page.text


def test_holdings_calculation_without_synced_nav(client):
    login(client)
    client.post(
        "/upload",
        data={"raw_text": "2024-01-02 005827 易方达蓝筹 buy 500 250 2.0000 0"},
    )
    client.post("/candidates/1/confirm", follow_redirects=False)
    page = client.get("/holdings")
    assert "005827" in page.text
    assert "250.0000" in page.text
    assert "¥500.00" in page.text


def test_backup_export(client):
    login(client)
    client.post(
        "/upload",
        data={"raw_text": "2024-01-02 005827 易方达蓝筹 buy 500 250 2.0000 0"},
    )
    client.post("/candidates/1/confirm", follow_redirects=False)
    response = client.get("/backup/export")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    data = response.json()
    assert data["version"] == 1
    assert data["transactions"][0]["fund_code"] == "005827"
    assert data["imports"][0]["raw_text"]


def test_ocr_import_to_candidate_flow(client, monkeypatch):
    import app.main
    from app.ocr import OcrResult

    login(client)
    monkeypatch.setattr(
        app.main,
        "recognize_file",
        lambda *_: OcrResult(
            text="2024-01-02 161725 招商中证白酒 buy 1000 500 2.0000 0",
            confidence=0.99,
        ),
    )
    response = client.post(
        "/upload",
        files={"file": ("trade.png", b"fake image", "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/imports/1"

    response = client.post("/imports/1/ocr", follow_redirects=False)
    assert response.status_code == 303
    detail = client.get("/imports/1")
    assert "招商中证白酒" in detail.text

    response = client.post("/imports/1/parse", follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/candidates")
    assert "161725" in page.text
    assert "pending" in page.text


def test_settings_page_saves_runtime_config(client):
    login(client)
    response = client.post(
        "/settings",
        data={
            "deepseek_api_key": "sk-test-secret",
            "deepseek_base_url": "https://api.deepseek.example",
            "deepseek_model": "deepseek-chat",
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
    page = client.get("/settings")
    assert "api.deepseek.example" in page.text
    assert "https://ocr.example/parse" in page.text
    assert "sk-test-secret" not in page.text
    assert "ocr-secret" not in page.text
    assert "末尾 cret" in page.text

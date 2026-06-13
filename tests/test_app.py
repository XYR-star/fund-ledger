from datetime import date
import importlib

from fastapi.testclient import TestClient
import pytest
from sqlmodel import Session, SQLModel, select


@pytest.fixture()
def app_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")
    monkeypatch.setenv("FUND_LEDGER_USERNAME", "admin")
    monkeypatch.setenv(
        "FUND_LEDGER_PASSWORD_HASH",
        "$2b$12$.F8VDZ2aTHPmBtR1XJEwsOS2W1AfpZFUumyJku6KtWdzRcb1MZaxm",
    )
    monkeypatch.setenv("FUND_LEDGER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FUND_LEDGER_DB", str(tmp_path / "fund-ledger.sqlite3"))

    import app.config
    import app.db
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.db)
    importlib.reload(app.main)

    SQLModel.metadata.drop_all(app.db.engine)
    SQLModel.metadata.create_all(app.db.engine)
    monkeypatch.setattr(app.main, "search_fund_safely", lambda *_: None)
    monkeypatch.setattr(app.main, "sync_nav_for_fund", lambda *_args, **_kwargs: (0, "disabled in tests"))
    monkeypatch.setattr(app.main, "fetch_public_dividend_rows", lambda *_args, **_kwargs: [])

    with TestClient(app.main.app) as client:
        client.post("/login", data={"username": "admin", "password": "changeme"})
        yield app.main, app.db, client


def seed_open_fund(session: Session, main, code: str = "005827", name: str = "易方达蓝筹精选混合"):
    from app.models import FundAlias, FundNav, FundRule, FundType

    session.add(FundAlias(keyword="易方达蓝筹", fund_code=code, fund_name=name, fund_type=FundType.open_fund, source="test"))
    session.add(FundRule(fund_code=code, fund_name=name, fund_type=FundType.open_fund, buy_confirm_days=1, sell_confirm_days=1))
    session.add(FundNav(fund_code=code, nav_date=date(2024, 1, 2), unit_nav=1.0))
    session.add(FundNav(fund_code=code, nav_date=date(2024, 1, 3), unit_nav=2.0))
    session.add(FundNav(fund_code=code, nav_date=date(2024, 1, 4), unit_nav=2.5))
    session.commit()


def parse_rows(session: Session, main, rows: list[list[str]]) -> int:
    from app.models import ImportDocument, ImportStatus

    doc = ImportDocument(file_name="screenshot.png", source_hash="hash", status=ImportStatus.ocr_done)
    session.add(doc)
    session.commit()
    session.refresh(doc)
    main.save_ocr_rows(session, doc, rows)
    return main.parse_document_candidates(session, doc.id)


def test_auto_post_high_quality_buy_uses_1500_cutoff_and_t_plus(app_ctx):
    main, db, _ = app_ctx
    from app.models import FundTransaction, TransactionCandidate

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        created = parse_rows(
            session,
            main,
            [["2024-01-02", "15:30:00", "成功", "易方达蓝筹", "买入", "金额1000元"]],
        )
        assert created == 1
        candidate = session.exec(select(TransactionCandidate)).one()
        assert candidate.status.value == "auto_ready"
        assert candidate.effective_nav_date == date(2024, 1, 3)
        assert candidate.confirm_date == date(2024, 1, 4)
        assert candidate.nav == 2.0
        assert candidate.share == 500.0

        main.post_candidate(session, candidate)
        session.commit()
        tx = session.exec(select(FundTransaction)).one()
        assert tx.amount_cny == 1000
        assert tx.share == 500


def test_business_timezone_and_cutoff_are_shanghai_wall_time(app_ctx):
    main, _, _ = app_ctx
    from app.models import FundRule

    rule = FundRule(fund_code="005827", cutoff_time="15:00")
    assert main.BUSINESS_TIMEZONE_NAME == "Asia/Shanghai"
    assert main.effective_nav_target_date(date(2024, 1, 2), main.parse_time("14:59:59"), rule) == date(2024, 1, 2)
    assert main.effective_nav_target_date(date(2024, 1, 2), main.parse_time("15:00:00"), rule) == date(2024, 1, 3)


def test_buy_share_uses_two_decimal_half_up_rounding(app_ctx):
    main, db, _ = app_ctx
    from app.models import FundNav, FundTransaction, TransactionCandidate

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        for nav in session.exec(select(FundNav)).all():
            session.delete(nav)
        session.flush()
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 2), unit_nav=3.0))
        session.commit()

        parse_rows(session, main, [["2024-01-02", "14:30:00", "成功", "易方达蓝筹", "买入", "金额1000元"]])
        candidate = session.exec(select(TransactionCandidate)).one()
        assert candidate.share == 333.33

        assert main.round_fund_share(1.005) == 1.01
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=main.TransactionAction.buy,
                amount_cny=1000,
                share=None,
                nav=3.0,
            )
        )
        session.commit()
        position = main.calculate_positions(session, include_closed=False)[0]
        assert position["share"] == 333.33


def test_sqlite_is_configured_for_concurrent_ocr_writes(app_ctx):
    _, db, _ = app_ctx
    from sqlmodel import text

    with Session(db.engine) as session:
        assert session.exec(text("PRAGMA journal_mode")).one()[0].lower() == "wal"
        assert session.exec(text("PRAGMA busy_timeout")).one()[0] >= 30000


def test_identical_rows_are_independent_operations(app_ctx):
    main, db, _ = app_ctx
    from app.models import FundTransaction, TransactionCandidate

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        row = ["2024-01-02", "14:30:00", "成功", "易方达蓝筹", "买入", "金额1000元"]
        assert parse_rows(session, main, [row, row]) == 2
        candidates = session.exec(select(TransactionCandidate).order_by(TransactionCandidate.id)).all()
        assert len(candidates) == 2
        for candidate in candidates:
            main.post_candidate(session, candidate)
        session.commit()
        assert len(session.exec(select(FundTransaction)).all()) == 2


def test_baidu_table_rows_use_columns_and_skip_headers(app_ctx):
    main, db, _ = app_ctx
    from app.models import FundAlias, FundRule, FundType, TransactionCandidate

    with Session(db.engine) as session:
        session.add(
            FundAlias(
                keyword="易方达全球成长精选混合(QDII)A(人民币份额)",
                fund_code="012345",
                fund_name="易方达全球成长精选混合(QDII)A",
                fund_type=FundType.open_fund,
                source="test",
            )
        )
        session.add(FundRule(fund_code="012345", fund_name="易方达全球成长精选混合(QDII)A", fund_type=FundType.open_fund))
        session.commit()
        created = parse_rows(
            session,
            main,
            [
                ["名称", "创建时间", "交易类型", "交易渠道", "份额", "金额", "交易账户", "状态", "操作"],
                ["易方达全球成长精选混合(QDII)A(人民币份额)", "2026-05-2620:40:31", "申购", "APP", "--", "20.00", "中国银行", "成功", "明细"],
                ["易方达全球成长精选混合(QDII)A(人民币份额)", "2026-05-2622:54:04", "赎回", "APP", "51.95", "--", "中国银行", "成功", "明细"],
                ["易方达全球成长精选混合(QDII)A(人民币份额)", "2026-05-2608:30:00", "红利再投资", "APP", "6.45", "--", "中国银行", "成功", "明细"],
            ],
        )
        assert created == 3
        candidates = session.exec(select(TransactionCandidate).order_by(TransactionCandidate.id)).all()
        assert [candidate.action.value for candidate in candidates] == ["buy", "sell", "dividend_reinvest"]
        assert candidates[0].amount_cny == 20.0
        assert candidates[0].share is None
        assert candidates[1].share == 51.95
        assert candidates[1].amount_cny is None
        assert candidates[2].share == 6.45


def test_fund_name_ocr_spacing_noise_matches_alias(app_ctx):
    main, db, _ = app_ctx
    from app.models import FundAlias, FundType, TransactionCandidate

    canonical = "测试中证海外中国互联网50ETF联接(QDII)A(人民币份额)"
    noisy = "测试中证海外中国互联网50ETF联接(QDII)A(人民 币份额)"
    with Session(db.engine) as session:
        session.add(
            FundAlias(
                keyword=canonical,
                fund_code="123456",
                fund_name="可能来自OCR的错误基金名",
                fund_type=FundType.open_fund,
                source="fund_map",
            )
        )
        session.commit()
        parse_rows(
            session,
            main,
            [[noisy, "2026-05-2614:40:31", "申购", "APP", "--", "20.00", "中国银行", "成功", "明细"]],
        )
        candidate = session.exec(select(TransactionCandidate)).one()
        assert candidate.fund_code == "123456"
        assert candidate.fund_name == canonical
        assert " " not in candidate.fund_name


def test_conversion_action_is_treated_as_sell(app_ctx):
    main, db, _ = app_ctx
    from app.models import FundAlias, FundRule, FundType, TransactionCandidate

    with Session(db.engine) as session:
        session.add(FundAlias(keyword="易方达蓝筹", fund_code="005827", fund_name="易方达蓝筹精选混合", fund_type=FundType.open_fund, source="test"))
        session.add(FundRule(fund_code="005827", fund_name="易方达蓝筹精选混合", fund_type=FundType.open_fund))
        session.commit()
        parse_rows(
            session,
            main,
            [["易方达蓝筹", "2026-05-2614:40:31", "转换", "APP", "12.34", "--", "中国银行", "成功", "明细"]],
        )
        candidate = session.exec(select(TransactionCandidate)).one()
        assert candidate.action.value == "sell"
        assert candidate.share == 12.34


def test_regular_sip_execution_is_buy_but_sip_setup_is_event(app_ctx):
    main, db, _ = app_ctx
    from app.models import TransactionCandidate

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        parse_rows(
            session,
            main,
            [
                ["易方达蓝筹", "2024-01-0214:40:31", "定投", "APP", "--", "10.00", "中国银行", "成功", "明细"],
                ["易方达蓝筹", "2024-01-0214:40:31", "开始定投", "APP", "--", "10.00", "中国银行", "成功", "明细"],
            ],
        )
        candidates = session.exec(select(TransactionCandidate).order_by(TransactionCandidate.id)).all()
        assert candidates[0].action.value == "buy"
        assert candidates[0].amount_cny == 10.0
        assert candidates[1].event_type.value == "sip_start"


def test_normalize_reclassifies_missing_action_from_raw_text(app_ctx):
    main, db, _ = app_ctx
    from app.models import RowStatus, TransactionCandidate

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        candidate = TransactionCandidate(
            row_status=RowStatus.success,
            fund_code="005827",
            fund_name="易方达蓝筹精选混合",
            fund_type=main.FundType.open_fund,
            trade_date=date(2024, 1, 2),
            amount_cny=10,
            raw_text="易方达蓝筹 2024-01-02 定投 -- 10.00 成功",
        )
        session.add(candidate)
        session.commit()
        session.refresh(candidate)
        main.normalize_candidate_for_posting(session, candidate)
        assert candidate.action.value == "buy"
        assert candidate.status.value == "auto_ready"


def test_cancelled_failed_and_sip_rows_become_events_not_transactions(app_ctx):
    main, db, client = app_ctx
    from app.models import FundEvent, FundTransaction, TransactionCandidate

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        parse_rows(
            session,
            main,
            [
                ["2024-01-02", "14:30:00", "撤销", "易方达蓝筹", "买入", "金额1000元"],
                ["2024-01-02", "14:31:00", "成功", "易方达蓝筹", "开始定投", "金额200元"],
                ["2024-01-02", "14:32:00", "失败", "易方达蓝筹", "赎回", "份额100份"],
            ],
        )
        candidates = session.exec(select(TransactionCandidate)).all()
        assert [c.status.value for c in candidates] == ["event", "event", "event"]

    response = client.post("/candidates/auto-post", follow_redirects=False)
    assert response.status_code == 303

    with Session(db.engine) as session:
        assert session.exec(select(FundTransaction)).all() == []
        events = session.exec(select(FundEvent).order_by(FundEvent.id)).all()
        assert [event.event_type.value for event in events] == ["ignored_status", "sip_start", "ignored_status"]


def test_sell_with_only_share_estimates_amount_and_fifo_fee(app_ctx):
    main, db, _ = app_ctx
    from app.models import FundFeeTier, FundTransaction, TransactionAction, TransactionCandidate

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        rule = session.get(main.FundRule, "005827")
        rule.sell_confirm_days = 1
        session.add(rule)
        session.add(FundFeeTier(fund_code="005827", min_holding_days=0, max_holding_days=7, redemption_fee_rate=0.015))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=1000,
                share=1000,
                nav=1,
            )
        )
        session.commit()

        parse_rows(session, main, [["2024-01-03", "14:00:00", "成功", "易方达蓝筹", "赎回", "份额100份"]])
        candidate = session.exec(select(TransactionCandidate)).one()
        assert candidate.amount_cny == 197
        assert candidate.fee == 3


def test_sell_fee_auto_syncs_redemption_tiers_when_missing(app_ctx, monkeypatch):
    main, db, _ = app_ctx
    from app.fund_rule_sync import SyncedRule
    from app.models import FundFeeTier, FundTransaction, TransactionAction, TransactionCandidate

    monkeypatch.setattr(
        main,
        "fetch_fund_rule_from_akshare",
        lambda code: SyncedRule(
            fund_code=code,
            fund_name="易方达蓝筹精选混合",
            buy_confirm_days=1,
            sell_confirm_days=1,
            fee_tiers=[(0, 7, 0.015), (7, None, 0.0)],
            source="test",
        ),
    )
    with Session(db.engine) as session:
        seed_open_fund(session, main)
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=1000,
                share=1000,
                nav=1,
            )
        )
        session.commit()
        assert session.exec(select(FundFeeTier)).all() == []

        parse_rows(session, main, [["2024-01-03", "14:00:00", "成功", "易方达蓝筹", "赎回", "份额100份"]])
        candidate = session.exec(select(TransactionCandidate)).one()
        assert candidate.amount_cny == 197
        assert candidate.fee == 3
        assert len(session.exec(select(FundFeeTier)).all()) == 2


def test_sell_fee_uses_unposted_buy_candidates_for_fifo(app_ctx):
    main, db, _ = app_ctx
    from app.models import FundAlias, FundFeeTier, FundNav, FundRule, FundType, TransactionCandidate

    with Session(db.engine) as session:
        session.add(FundAlias(keyword="测试国家安全", fund_code="123457", fund_name="测试国家安全沪港深股票A", fund_type=FundType.open_fund, source="test"))
        session.add(FundRule(fund_code="123457", fund_name="测试国家安全沪港深股票A", fund_type=FundType.open_fund, buy_confirm_days=1, sell_confirm_days=1))
        session.add(FundFeeTier(fund_code="123457", min_holding_days=30, max_holding_days=180, redemption_fee_rate=0.005))
        session.add(FundNav(fund_code="123457", nav_date=date(2026, 1, 26), unit_nav=2.7243))
        session.add(FundNav(fund_code="123457", nav_date=date(2026, 1, 27), unit_nav=2.7243))
        session.add(FundNav(fund_code="123457", nav_date=date(2026, 4, 23), unit_nav=3.4298))
        session.add(FundNav(fund_code="123457", nav_date=date(2026, 4, 24), unit_nav=3.4298))
        session.commit()
        parse_rows(
            session,
            main,
            [
                ["测试国家安全沪港深股票A", "2026-01-2417:54:12", "申购", "APP", "--", "100.00", "中国银行", "成功", "明细"],
                ["测试国家安全沪港深股票A", "2026-04-2221:20:41", "赎回", "APP", "11.21", "--", "中国银行", "成功", "明细"],
            ],
        )
        sell = session.exec(select(TransactionCandidate).where(TransactionCandidate.action == main.TransactionAction.sell)).one()
        assert sell.share == 11.21
        assert sell.nav == 3.4298
        assert sell.fee == 0.19
        assert sell.amount_cny == 38.26


def test_import_detail_shows_candidate_fee(app_ctx):
    main, db, client = app_ctx
    from app.models import FundFeeTier, FundTransaction, ImportDocument, ImportStatus, TransactionAction

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        session.add(FundFeeTier(fund_code="005827", min_holding_days=0, max_holding_days=7, redemption_fee_rate=0.015))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=1000,
                share=1000,
                nav=1,
            )
        )
        doc = ImportDocument(file_name="sell.png", source_hash="h", status=ImportStatus.ocr_done)
        session.add(doc)
        session.commit()
        session.refresh(doc)
        doc_id = doc.id
        main.save_ocr_rows(session, doc, [["2024-01-03", "14:00:00", "成功", "易方达蓝筹", "赎回", "份额100份"]])
        main.parse_document_candidates(session, doc_id)

    response = client.get(f"/imports/{doc_id}")
    assert response.status_code == 200
    assert "<th>费用</th>" in response.text
    assert "<td>3" in response.text


def test_import_auto_post_only_posts_that_document(app_ctx):
    main, db, client = app_ctx
    from app.models import FundTransaction, ImportDocument, ImportStatus, TransactionCandidate

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        doc_one = ImportDocument(file_name="one.png", source_hash="one", status=ImportStatus.ocr_done)
        doc_two = ImportDocument(file_name="two.png", source_hash="two", status=ImportStatus.ocr_done)
        session.add(doc_one)
        session.add(doc_two)
        session.commit()
        session.refresh(doc_one)
        session.refresh(doc_two)
        main.save_ocr_rows(session, doc_one, [["2024-01-02", "14:00:00", "成功", "易方达蓝筹", "买入", "金额1000元"]])
        main.save_ocr_rows(session, doc_two, [["2024-01-02", "14:00:00", "成功", "易方达蓝筹", "买入", "金额500元"]])
        main.parse_document_candidates(session, doc_one.id)
        main.parse_document_candidates(session, doc_two.id)
        doc_one_id = doc_one.id
        doc_two_id = doc_two.id

    response = client.post(f"/imports/{doc_one_id}/auto-post", follow_redirects=False)
    assert response.status_code == 303

    with Session(db.engine) as session:
        transactions = session.exec(select(FundTransaction)).all()
        assert len(transactions) == 1
        assert transactions[0].amount_cny == 1000
        statuses = {
            candidate.document_id: candidate.status.value
            for candidate in session.exec(select(TransactionCandidate).order_by(TransactionCandidate.document_id)).all()
        }
        assert statuses[doc_one_id] == "posted"
        assert statuses[doc_two_id] == "auto_ready"


def test_transactions_page_groups_are_collapsible(app_ctx):
    main, db, client = app_ctx
    from app.models import FundTransaction, TransactionAction

    with Session(db.engine) as session:
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=1000,
                share=1000,
                nav=1,
            )
        )
        session.commit()

    response = client.get("/transactions")
    assert response.status_code == 200
    assert '<details class="panel stack ledger-group"' in response.text
    assert "易方达蓝筹精选混合 005827" in response.text
    assert "1 条" in response.text


def test_transactions_page_includes_dividend_method_events(app_ctx):
    main, db, client = app_ctx
    from app.models import FundEvent, EventType, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.dividend,
                amount_cny=10,
            )
        )
        session.add(
            FundEvent(
                event_type=EventType.dividend_method,
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                event_date=date(2024, 1, 3),
                raw_text="易方达蓝筹 修改分红方式 红利再投资 成功",
            )
        )
        session.commit()

    response = client.get("/transactions")
    assert response.status_code == 200
    assert "dividend_method" in response.text
    assert "修改分红方式" in response.text
    assert "事件" in response.text


def test_charts_page_shows_only_active_open_funds(app_ctx):
    main, db, client = app_ctx
    from app.models import FundNav, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 2), unit_nav=1.0))
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 3), unit_nav=1.2))
        session.add(FundNav(fund_code="000001", nav_date=date(2024, 1, 2), unit_nav=1.0))
        session.add(FundNav(fund_code="000001", nav_date=date(2024, 1, 3), unit_nav=1.1))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="000001",
                fund_name="已清仓基金",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="000001",
                fund_name="已清仓基金",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 3),
                action=TransactionAction.sell,
                amount_cny=110,
                share=100,
                nav=1.1,
            )
        )
        session.commit()

    response = client.get("/charts")
    assert response.status_code == 200
    assert "易方达蓝筹精选混合 005827" in response.text
    assert "已清仓基金" not in response.text
    assert 'data-range="1y"' in response.text
    assert "/static/fund_charts.js" in response.text
    nav = client.get("/holdings")
    assert 'href="/charts">曲线</a>' in nav.text


def test_fund_chart_markers_include_amount_for_scaled_points(app_ctx):
    main, _, _ = app_ctx
    from app.models import FundNav, FundTransaction, TransactionAction

    chart = main.build_fund_chart(
        [
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=1000,
                share=1000,
                nav=1,
            ),
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 3),
                action=TransactionAction.sell,
                amount_cny=None,
                share=100,
                nav=2,
            ),
        ],
        [
            FundNav(fund_code="005827", nav_date=date(2024, 1, 2), unit_nav=1),
            FundNav(fund_code="005827", nav_date=date(2024, 1, 3), unit_nav=2),
        ],
    )

    assert [marker["amount"] for marker in chart["markers"]] == [1000, 200]


def test_tiny_residual_after_sell_is_treated_as_closed(app_ctx):
    main, db, _ = app_ctx
    from app.models import FundNav, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 3), unit_nav=1.0))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 3),
                action=TransactionAction.sell,
                amount_cny=99.95,
                share=99.95,
                nav=1,
            )
        )
        session.commit()

        positions = main.calculate_positions(session)
        assert positions[0]["is_closed"] is True
        assert positions[0]["share"] == 0
        assert main.calculate_positions(session, include_closed=False) == []


def test_small_buy_without_sell_stays_active(app_ctx):
    main, db, _ = app_ctx
    from app.models import FundNav, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 2), unit_nav=1.0))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=0.1,
                share=0.1,
                nav=1,
            )
        )
        session.commit()

        positions = main.calculate_positions(session)
        assert positions[0]["is_closed"] is False
        assert positions[0]["share"] == 0.1


def test_holdings_show_unit_cost(app_ctx):
    main, db, client = app_ctx
    from app.models import FundTransaction, TransactionAction

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=123,
                share=100,
                nav=1.23,
            )
        )
        session.commit()

        position = main.calculate_positions(session, include_closed=False)[0]
        assert position["unit_cost"] == 1.23

    response = client.get("/holdings")
    assert response.status_code == 200
    assert "单位成本" in response.text
    assert "1.2300" in response.text


def test_position_cost_uses_average_cost_and_cash_dividend_reduces_cost(app_ctx):
    main, db, _ = app_ctx
    from app.models import FundNav, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 5), unit_nav=1.0))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 1),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=120,
                share=100,
                nav=1.2,
            )
        )
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 3),
                action=TransactionAction.sell,
                amount_cny=110,
                share=100,
                nav=1.1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 4),
                action=TransactionAction.dividend,
                amount_cny=10,
                nav=1,
            )
        )
        session.commit()

        position = main.calculate_positions(session, include_closed=False)[0]
        assert position["share"] == 100
        assert position["cost"] == 100
        assert position["realized_profit"] == 0
        assert position["profit"] == 0
        assert position["total_profit"] == 0
        assert position["unit_cost"] == 1.0


def test_holdings_label_holding_and_total_profit(app_ctx):
    main, db, client = app_ctx
    from app.models import FundNav, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 2), unit_nav=1.2))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 1),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.commit()

    response = client.get("/holdings")
    assert response.status_code == 200
    assert "持仓收益" in response.text
    assert "累计收益" in response.text


def test_sync_public_dividend_reinvests_once_and_skips_duplicates(app_ctx, monkeypatch):
    main, db, _ = app_ctx
    from app.models import FundNav, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 10), unit_nav=2.0))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.commit()

        monkeypatch.setattr(
            main,
            "fetch_public_dividend_rows",
            lambda years: [
                {
                    "fund_code": "005827",
                    "fund_name": "易方达蓝筹精选混合",
                    "register_date": date(2024, 1, 10),
                    "pay_date": date(2024, 1, 11),
                    "dividend_per_share": 0.1,
                }
            ],
        )

        result = main.sync_active_fund_dividends(session, years=[2024])
        assert result["posted"] == 1

        tx = session.exec(select(FundTransaction).where(FundTransaction.action == TransactionAction.dividend_reinvest)).one()
        assert tx.source_file == "auto_dividend_sync"
        assert tx.share == 5.0
        assert tx.amount_cny is None
        assert tx.nav == 2.0
        assert tx.trade_date == date(2024, 1, 11)

        result = main.sync_active_fund_dividends(session, years=[2024])
        assert result["posted"] == 0
        assert len(session.exec(select(FundTransaction).where(FundTransaction.action == TransactionAction.dividend_reinvest)).all()) == 1


def test_sync_public_dividend_uses_cash_when_rule_says_cash(app_ctx, monkeypatch):
    main, db, _ = app_ctx
    from app.models import FundRule, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        rule = session.get(FundRule, "005827")
        rule.dividend_method = "修改分红方式为现金分红"
        session.add(rule)
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.commit()

        monkeypatch.setattr(
            main,
            "fetch_public_dividend_rows",
            lambda years: [
                {
                    "fund_code": "005827",
                    "fund_name": "易方达蓝筹精选混合",
                    "register_date": date(2024, 1, 10),
                    "pay_date": date(2024, 1, 11),
                    "dividend_per_share": 0.1,
                }
            ],
        )

        result = main.sync_active_fund_dividends(session, years=[2024])
        assert result["posted"] == 1

        tx = session.exec(select(FundTransaction).where(FundTransaction.action == TransactionAction.dividend)).one()
        assert tx.source_file == "auto_dividend_sync"
        assert tx.amount_cny == 10.0
        assert tx.share is None
        assert tx.trade_date == date(2024, 1, 11)


def test_sync_public_dividend_only_posts_after_latest_imported_transaction(app_ctx, monkeypatch):
    main, db, _ = app_ctx
    from app.models import FundNav, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 6, 10), unit_nav=2.0))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 3, 16),
                action=TransactionAction.dividend,
                amount_cny=10,
                raw_text="现金分红 已导入",
            )
        )
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 5, 31),
                action=TransactionAction.buy,
                amount_cny=20,
                share=10,
                nav=2,
                raw_text="五月底导入流水",
            )
        )
        session.commit()

        monkeypatch.setattr(
            main,
            "fetch_public_dividend_rows",
            lambda years: [
                {
                    "fund_code": "005827",
                    "fund_name": "易方达蓝筹精选混合",
                    "register_date": date(2024, 3, 13),
                    "pay_date": date(2024, 3, 16),
                    "dividend_per_share": 0.1,
                },
                {
                    "fund_code": "005827",
                    "fund_name": "易方达蓝筹精选混合",
                    "register_date": date(2024, 6, 8),
                    "pay_date": date(2024, 6, 9),
                    "dividend_per_share": 0.1,
                },
            ],
        )

        result = main.sync_active_fund_dividends(session, years=[2024])
        assert result["posted"] == 1

        auto_txs = session.exec(select(FundTransaction).where(FundTransaction.source_file == "auto_dividend_sync")).all()
        assert len(auto_txs) == 1
        assert auto_txs[0].trade_date == date(2024, 6, 9)
        assert auto_txs[0].action == TransactionAction.dividend_reinvest
        assert auto_txs[0].share == 5.5


def test_sync_public_dividend_skips_existing_auto_row_on_register_date(app_ctx, monkeypatch):
    main, db, _ = app_ctx
    from app.models import FundNav, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 6, 8), unit_nav=2.0))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 5, 31),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 6, 8),
                action=TransactionAction.dividend_reinvest,
                share=5,
                nav=2,
                source_file="auto_dividend_sync",
            )
        )
        session.commit()

        monkeypatch.setattr(
            main,
            "fetch_public_dividend_rows",
            lambda years: [
                {
                    "fund_code": "005827",
                    "fund_name": "易方达蓝筹精选混合",
                    "register_date": date(2024, 6, 8),
                    "pay_date": date(2024, 6, 9),
                    "dividend_per_share": 0.1,
                }
            ],
        )

        result = main.sync_active_fund_dividends(session, years=[2024])
        assert result["posted"] == 0
        auto_txs = session.exec(select(FundTransaction).where(FundTransaction.source_file == "auto_dividend_sync")).all()
        assert len(auto_txs) == 1


def test_dividend_sync_route_starts_background_job_without_inline_sync(app_ctx, monkeypatch):
    main, db, client = app_ctx
    from app.app_settings import runtime_settings

    scheduled = []

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return object()

    monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(
        main,
        "run_dividend_sync_background",
        lambda: (_ for _ in ()).throw(AssertionError("should run in background")),
    )

    response = client.post("/dividends/sync", follow_redirects=False)
    assert response.status_code == 303
    assert "message=" in response.headers["location"]
    assert scheduled

    with Session(db.engine) as session:
        config = runtime_settings(session)
        assert config["DIVIDEND_SYNC_RUNNING"] == "true"
        assert config["DIVIDEND_SYNC_LAST_RESULT"] == "同步中"


def test_refresh_eaccount_reconciliations_after_auto_dividend(app_ctx):
    main, db, _ = app_ctx
    from app.models import EAccountHolding, EAccountImport, FundNav, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        imported = EAccountImport(file_name="snapshot.csv", row_count=1, mismatch_count=1)
        session.add(imported)
        session.flush()
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 10), unit_nav=2.0))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.add(
            EAccountHolding(
                import_id=imported.id,
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                official_share=105,
                share_date=date(2024, 1, 10),
                nav=2,
                nav_date=date(2024, 1, 10),
                settlement_value=210,
                local_share=100,
                local_market_value=200,
                share_diff=5,
                market_value_diff=10,
                status="mismatch",
                issue_summary="份额差异 5.00",
            )
        )
        session.commit()

        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 10),
                action=TransactionAction.dividend_reinvest,
                share=5,
                nav=2,
                source_file="auto_dividend_sync",
            )
        )
        session.commit()

        refreshed = main.refresh_eaccount_reconciliations(session)
        assert refreshed == 1

        holding = session.exec(select(EAccountHolding)).one()
        imported = session.get(EAccountImport, imported.id)
        assert holding.local_share == 105
        assert holding.local_market_value == 210
        assert holding.share_diff == 0
        assert holding.status == "matched"
        assert imported.matched_count == 1
        assert imported.mismatch_count == 0


def test_holdings_page_shows_dividend_sync_result(app_ctx):
    _, db, client = app_ctx
    from app.app_settings import save_settings

    with Session(db.engine) as session:
        save_settings(
            session,
            {
                "DIVIDEND_SYNC_LAST_RUN_AT": "2026-06-13 20:45:25",
                "DIVIDEND_SYNC_LAST_FINISHED_AT": "2026-06-13 20:47:31",
                "DIVIDEND_SYNC_LAST_RESULT": "分红同步完成: 入账 1 条，跳过 10429 条",
            },
        )

    response = client.get("/holdings")
    assert response.status_code == 200
    assert "上次分红同步" in response.text
    assert "入账 1 条" in response.text


def test_later_ocr_dividend_matching_auto_sync_is_ignored(app_ctx):
    main, db, _ = app_ctx
    from app.models import CandidateStatus, FundTransaction, TransactionAction, TransactionCandidate

    with Session(db.engine) as session:
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 10),
                action=TransactionAction.dividend_reinvest,
                share=5.0,
                nav=2.0,
                source_file="auto_dividend_sync",
            )
        )
        candidate = TransactionCandidate(
            status=CandidateStatus.auto_ready,
            row_status=main.RowStatus.success,
            fund_code="005827",
            fund_name="易方达蓝筹精选混合",
            fund_type=main.FundType.open_fund,
            trade_date=date(2024, 1, 10),
            action=TransactionAction.dividend_reinvest,
            share=5.0,
            nav=2.0,
            raw_text="红利再投资 易方达蓝筹 5.00 成功",
        )
        session.add(candidate)
        session.commit()

        main.post_candidate(session, candidate)
        session.commit()

        transactions = session.exec(select(FundTransaction)).all()
        assert len(transactions) == 1
        assert candidate.status == CandidateStatus.ignored
        assert candidate.review_reason == "已由系统分红同步入账"
        assert candidate.posted_transaction_id == transactions[0].id


def test_holdings_show_manual_platform_cards(app_ctx):
    main, db, client = app_ctx
    from app.models import FundRule, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        rule = session.get(FundRule, "005827")
        rule.platform = "易方达"
        session.add(rule)
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.commit()

    response = client.get("/holdings")
    assert response.status_code == 200
    assert "基金平台" in response.text
    assert "易方达 · 1 只" in response.text
    assert "持仓收益" in response.text


def test_eaccount_import_reconciles_official_holdings(app_ctx):
    main, db, client = app_ctx
    from app.models import EAccountHolding, EAccountImport, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.commit()

    csv = "\n".join(
        [
            "序号,基金代码,基金名称,基金账户,持有份额,份额日期,基金净值,净值日期,资产情况(结算市值),结算市值",
            "1,005827,易方达蓝筹精选混合,FA001,100.00,2024-01-02,2.50,2024-01-04,250.00,250.00",
        ]
    )
    response = client.post(
        "/eaccount/import",
        files={"file": ("eaccount.csv", csv.encode("utf-8-sig"), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db.engine) as session:
        imported = session.exec(select(EAccountImport)).one()
        holding = session.exec(select(EAccountHolding)).one()
        assert imported.row_count == 1
        assert imported.matched_count == 1
        assert imported.mismatch_count == 0
        assert holding.status == "matched"
        assert holding.local_share == 100
        assert holding.share_diff == 0

    page = client.get("/eaccount")
    assert page.status_code == 200
    assert "基金 E 账户对账" in page.text
    assert "易方达蓝筹精选混合" in page.text
    assert "matched" in page.text
    assert "分红方式" not in page.text


def test_eaccount_import_marks_mismatched_holdings(app_ctx):
    main, db, client = app_ctx
    from app.models import EAccountHolding, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.commit()

    csv = "\n".join(
        [
            "基金代码,基金名称,持有份额,份额日期,基金净值,净值日期,结算市值",
            "005827,易方达蓝筹精选混合,98.00,2024-01-02,1.00,2024-01-02,98.00",
        ]
    )
    response = client.post(
        "/eaccount/import",
        files={"file": ("eaccount.csv", csv.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db.engine) as session:
        holding = session.exec(select(EAccountHolding)).one()
        assert holding.status == "mismatch"
        assert holding.share_diff == -2.0
        assert "份额差异" in holding.issue_summary


def test_eaccount_import_merges_same_fund_rows_before_reconciling(app_ctx):
    main, db, client = app_ctx
    from app.models import EAccountHolding, EAccountImport, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=517.74,
                share=517.74,
                nav=1,
            )
        )
        session.commit()

    csv = "\n".join(
        [
            "基金代码,基金名称,持有份额,份额日期,基金净值,净值日期,结算市值",
            "005827,易方达蓝筹精选混合,301.18,2024-01-02,1.00,2024-01-02,301.18",
            "005827,易方达蓝筹精选混合,216.56,2024-01-02,1.00,2024-01-02,216.56",
        ]
    )
    response = client.post(
        "/eaccount/import",
        files={"file": ("eaccount.csv", csv.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db.engine) as session:
        imported = session.exec(select(EAccountImport)).one()
        holding = session.exec(select(EAccountHolding)).one()
        assert imported.row_count == 1
        assert imported.matched_count == 1
        assert imported.mismatch_count == 0
        assert holding.status == "matched"
        assert holding.official_share == 517.74
        assert holding.settlement_value == 517.74
        assert holding.local_share == 517.74


def test_eaccount_cleanup_merges_existing_duplicate_fund_holdings(app_ctx):
    _, db, _ = app_ctx
    from app.models import EAccountHolding, EAccountImport

    with Session(db.engine) as session:
        imported = EAccountImport(file_name="old.xlsx", row_count=2, mismatch_count=2)
        session.add(imported)
        session.flush()
        session.add(
            EAccountHolding(
                import_id=imported.id,
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                official_share=301.18,
                share_date=date(2024, 1, 2),
                nav=1,
                nav_date=date(2024, 1, 2),
                settlement_value=301.18,
                local_share=517.74,
                local_market_value=517.74,
                share_diff=-216.56,
                market_value_diff=-216.56,
                status="mismatch",
                issue_summary="份额差异 -216.56",
            )
        )
        session.add(
            EAccountHolding(
                import_id=imported.id,
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                official_share=216.56,
                share_date=date(2024, 1, 2),
                nav=1,
                nav_date=date(2024, 1, 2),
                settlement_value=216.56,
                local_share=517.74,
                local_market_value=517.74,
                share_diff=-301.18,
                market_value_diff=-301.18,
                status="mismatch",
                issue_summary="份额差异 -301.18",
            )
        )
        session.commit()

    db.merge_existing_eaccount_holdings()

    with Session(db.engine) as session:
        imported = session.get(EAccountImport, 1)
        holding = session.exec(select(EAccountHolding)).one()
        assert imported.row_count == 1
        assert imported.matched_count == 1
        assert imported.mismatch_count == 0
        assert holding.official_share == 517.74
        assert holding.settlement_value == 517.74
        assert holding.share_diff == 0
        assert holding.market_value_diff == 0
        assert holding.status == "matched"


def test_eaccount_import_reconciles_against_snapshot_date_not_current_position(app_ctx):
    main, db, client = app_ctx
    from app.models import EAccountHolding, FundNav, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 6), unit_nav=3.0))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 5),
                action=TransactionAction.sell,
                amount_cny=150,
                share=50,
                nav=3,
            )
        )
        session.commit()

    csv = "\n".join(
        [
            "基金代码,基金名称,持有份额,份额日期,基金净值,净值日期,结算市值",
            "005827,易方达蓝筹精选混合,100.00,2024-01-04,2.50,2024-01-04,250.00",
        ]
    )
    response = client.post(
        "/eaccount/import",
        files={"file": ("snapshot.csv", csv.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db.engine) as session:
        holding = session.exec(select(EAccountHolding)).one()
        assert holding.status == "matched"
        assert holding.local_share == 100
        assert holding.local_market_value == 250
        assert holding.share_diff == 0


def test_eaccount_import_skips_rows_without_fund_identity(app_ctx):
    _, db, client = app_ctx
    from app.models import EAccountHolding, EAccountImport

    csv = "\n".join(
        [
            "序号,基金代码,基金名称,持有份额,份额日期,基金净值,净值日期,结算市值",
            "1,,,,,,,",
            "2,005827,易方达蓝筹精选混合,100.00,2024-01-02,1.00,2024-01-02,100.00",
        ]
    )
    response = client.post(
        "/eaccount/import",
        files={"file": ("eaccount.csv", csv.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db.engine) as session:
        imported = session.exec(select(EAccountImport)).one()
        holdings = session.exec(select(EAccountHolding)).all()
        assert imported.row_count == 1
        assert len(holdings) == 1
        assert holdings[0].fund_code == "005827"


def test_eaccount_cleanup_removes_existing_nan_placeholder_rows(app_ctx):
    _, db, _ = app_ctx
    from app.models import EAccountHolding, EAccountImport

    with Session(db.engine) as session:
        imported = EAccountImport(file_name="old.xlsx", row_count=2, missing_count=2)
        session.add(imported)
        session.flush()
        session.add(
            EAccountHolding(
                import_id=imported.id,
                fund_code="000000",
                fund_name="nan",
                status="missing",
                issue_summary="系统缺少持仓",
            )
        )
        session.add(
            EAccountHolding(
                import_id=imported.id,
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                official_share=100,
                status="missing",
                issue_summary="系统缺少持仓",
            )
        )
        session.commit()

    db.cleanup_invalid_eaccount_holdings()

    with Session(db.engine) as session:
        imported = session.get(EAccountImport, 1)
        holdings = session.exec(select(EAccountHolding).order_by(EAccountHolding.fund_code)).all()
        assert len(holdings) == 1
        assert holdings[0].fund_code == "005827"
        assert imported.row_count == 1
        assert imported.missing_count == 1


def test_eaccount_versions_keep_each_import_and_can_be_reopened(app_ctx):
    main, db, client = app_ctx
    from app.models import EAccountImport, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.commit()

    headers = "基金代码,基金名称,持有份额,份额日期,基金净值,净值日期,结算市值"
    first = f"{headers}\n005827,易方达蓝筹精选混合,100.00,2024-01-02,2.50,2024-01-04,250.00"
    second = f"{headers}\n005827,易方达蓝筹精选混合,98.00,2024-01-03,2.50,2024-01-04,245.00"
    client.post("/eaccount/import", files={"file": ("v1.csv", first.encode("utf-8"), "text/csv")})
    client.post("/eaccount/import", files={"file": ("v2.csv", second.encode("utf-8"), "text/csv")})

    with Session(db.engine) as session:
        versions = session.exec(select(EAccountImport).order_by(EAccountImport.id)).all()
        first_id, second_id = versions[0].id, versions[1].id

    latest_page = client.get("/eaccount")
    assert latest_page.status_code == 200
    assert f"版本 #{second_id}" in latest_page.text
    assert f'href="/eaccount/{first_id}"' in latest_page.text
    assert f'href="/eaccount/{second_id}"' in latest_page.text

    first_page = client.get(f"/eaccount/{first_id}")
    assert first_page.status_code == 200
    assert f"版本 #{first_id}" in first_page.text
    assert "v1.csv" in first_page.text
    assert "v2.csv" in first_page.text
    assert "250.00" in first_page.text


def test_eaccount_schema_migration_drops_removed_required_columns(app_ctx):
    main, db, _ = app_ctx
    from sqlmodel import text

    with Session(db.engine) as session:
        session.exec(text("ALTER TABLE eaccountholding ADD COLUMN share_category VARCHAR NOT NULL DEFAULT ''"))
        session.exec(text("ALTER TABLE eaccountholding ADD COLUMN manager VARCHAR NOT NULL DEFAULT ''"))
        session.exec(text("ALTER TABLE eaccountholding ADD COLUMN sales_agency VARCHAR NOT NULL DEFAULT ''"))
        session.exec(text("ALTER TABLE eaccountholding ADD COLUMN trading_account VARCHAR NOT NULL DEFAULT ''"))
        session.exec(text("ALTER TABLE eaccountholding ADD COLUMN dividend_method VARCHAR NOT NULL DEFAULT ''"))
        session.commit()

    db.migrate_eaccount_holding_schema()

    with Session(db.engine) as session:
        columns = [row[1] for row in session.exec(text("PRAGMA table_info(eaccountholding)")).all()]
        assert "share_category" not in columns
        assert "manager" not in columns

        imported = main.import_eaccount_holdings(
            session,
            "eaccount.csv",
            "基金代码,基金名称,持有份额\n005827,易方达蓝筹精选混合,100.00".encode("utf-8"),
        )
        assert imported.row_count == 1


def test_sell_without_prior_buy_is_marked_incomplete(app_ctx):
    main, db, _ = app_ctx
    from app.models import FundNav, FundTransaction, TransactionAction

    with Session(db.engine) as session:
        session.add(FundNav(fund_code="005827", nav_date=date(2024, 1, 2), unit_nav=1.0))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=main.FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.sell,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.commit()

        position = main.calculate_positions(session)[0]
        assert position["is_closed"] is True
        assert position["incomplete_history"] is True
        assert position["oversold_share"] == 100


def test_candidate_normalization_auto_syncs_missing_nav(app_ctx, monkeypatch):
    main, db, _ = app_ctx
    from app.models import FundNav, TransactionCandidate

    def fake_sync(session, code, *_args, **_kwargs):
        session.add(FundNav(fund_code=code, nav_date=date(2024, 1, 3), unit_nav=2.0, source="fake"))
        session.add(FundNav(fund_code=code, nav_date=date(2024, 1, 4), unit_nav=2.5, source="fake"))
        session.commit()
        return 2, None

    monkeypatch.setattr(main, "sync_nav_for_fund", fake_sync)
    with Session(db.engine) as session:
        seed_open_fund(session, main)
        session.exec(select(FundNav)).all()
        for nav in session.exec(select(FundNav)).all():
            session.delete(nav)
        session.commit()

        parse_rows(session, main, [["2024-01-02", "15:30:00", "成功", "易方达蓝筹", "买入", "金额1000元"]])
        candidate = session.exec(select(TransactionCandidate)).one()
        assert candidate.status.value == "auto_ready"
        assert candidate.effective_nav_date == date(2024, 1, 3)
        assert candidate.confirm_date == date(2024, 1, 4)
        assert candidate.nav == 2.0
        assert candidate.share == 500.0


def test_etf_and_money_fund_post_but_do_not_enter_holdings(app_ctx):
    main, db, client = app_ctx
    from app.models import FundAlias, FundRule, FundTransaction, FundType

    with Session(db.engine) as session:
        session.add(FundAlias(keyword="沪深300ETF", fund_code="510300", fund_name="沪深300ETF", fund_type=FundType.etf, source="test"))
        session.add(FundRule(fund_code="510300", fund_name="沪深300ETF", fund_type=FundType.etf))
        session.add(FundAlias(keyword="现金宝货币", fund_code="000000", fund_name="现金宝货币", fund_type=FundType.money_fund, source="test"))
        session.add(FundRule(fund_code="000000", fund_name="现金宝货币", fund_type=FundType.money_fund))
        session.commit()
        parse_rows(
            session,
            main,
            [
                ["2024-01-02", "14:00:00", "成功", "沪深300ETF", "买入", "金额1000元"],
                ["2024-01-02", "14:00:00", "成功", "现金宝货币", "买入", "金额500元"],
            ],
        )

    client.post("/candidates/auto-post", follow_redirects=False)

    with Session(db.engine) as session:
        assert len(session.exec(select(FundTransaction)).all()) == 2
        assert main.calculate_positions(session) == []


def test_baidu_table_ocr_entry_saves_rows_and_candidates(app_ctx, monkeypatch, tmp_path):
    main, db, client = app_ctx
    from app.models import FundTransaction, ImportDocument, OcrRow, TransactionCandidate
    from app.ocr import OcrResult

    with Session(db.engine) as session:
        seed_open_fund(session, main)

    image = tmp_path / "shot.png"
    image.write_bytes(b"fake")
    monkeypatch.setattr(
        main,
        "recognize_file",
        lambda *_: OcrResult(
            text="ocr",
            rows=[["2024-01-02", "14:00:00", "成功", "易方达蓝筹", "买入", "金额1000元"]],
        ),
    )

    with Session(db.engine) as session:
        doc = ImportDocument(file_name="shot.png", source_file=str(image), source_hash="h")
        session.add(doc)
        session.commit()
        session.refresh(doc)
        doc_id = doc.id

    response = client.post(f"/imports/{doc_id}/ocr", follow_redirects=False)
    assert response.status_code == 303
    with Session(db.engine) as session:
        assert session.exec(select(OcrRow)).one().raw_text.endswith("金额1000元")
        assert session.exec(select(TransactionCandidate)).one().status.value == "posted"
        assert session.exec(select(FundTransaction)).one().amount_cny == 1000


def test_imports_page_has_bulk_ocr_and_json_endpoint(app_ctx, monkeypatch, tmp_path):
    main, db, client = app_ctx
    from app.models import ImportDocument, ImportStatus, TransactionCandidate
    from app.ocr import OcrResult

    image = tmp_path / "shot.png"
    image.write_bytes(b"fake")
    monkeypatch.setattr(
        main,
        "recognize_file",
        lambda *_: OcrResult(text="ocr", rows=[["2024-01-02", "14:00:00", "成功", "未知基金", "买入", "金额1000元"]]),
    )
    with Session(db.engine) as session:
        doc = ImportDocument(file_name="shot.png", source_file=str(image), source_hash="h", status=ImportStatus.uploaded)
        session.add(doc)
        session.commit()
        session.refresh(doc)
        doc_id = doc.id

    page = client.get("/imports")
    assert page.status_code == 200
    assert "一键 OCR (1)" in page.text
    assert "删除" in page.text
    assert f"{doc_id}" in page.text

    response = client.post(f"/imports/{doc_id}/ocr-json")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    with Session(db.engine) as session:
        assert session.get(ImportDocument, doc_id).status.value == "parsed"
        assert session.exec(select(TransactionCandidate)).one().review_reason


def test_import_delete_removes_document_rows_and_candidates(app_ctx, tmp_path):
    main, db, client = app_ctx
    from app.models import ImportDocument, OcrRow, TransactionCandidate

    image = tmp_path / "shot.png"
    image.write_bytes(b"fake")
    with Session(db.engine) as session:
        seed_open_fund(session, main)
        doc = ImportDocument(file_name="shot.png", source_file=str(image), source_hash="h")
        session.add(doc)
        session.commit()
        session.refresh(doc)
        doc_id = doc.id
        main.save_ocr_rows(session, doc, [["2024-01-02", "14:00:00", "成功", "易方达蓝筹", "买入", "金额1000元"]])
        main.parse_document_candidates(session, doc_id)

    response = client.post(f"/imports/{doc_id}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert not image.exists()
    with Session(db.engine) as session:
        assert session.get(ImportDocument, doc_id) is None
        assert session.exec(select(OcrRow)).all() == []
        assert session.exec(select(TransactionCandidate)).all() == []


def test_core_pages_render(app_ctx):
    _, _, client = app_ctx

    for path in ["/candidates", "/imports", "/upload", "/transactions", "/events", "/holdings", "/charts", "/eaccount", "/funds", "/settings"]:
        response = client.get(path)
        assert response.status_code == 200, path


def test_funds_rules_table_keeps_table_cells_for_aligned_borders(app_ctx):
    _, _, client = app_ctx

    response = client.get("/funds")
    assert response.status_code == 200
    assert '<td class="actions">' not in response.text
    assert 'class="rule-actions"' in response.text
    assert 'class="rule-classify-form"' in response.text


def test_single_upload_endpoint_creates_document(app_ctx):
    _, db, client = app_ctx
    from app.models import ImportDocument

    response = client.post("/upload/single", files={"file": ("one.png", b"image-bytes", "image/png")})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    with Session(db.engine) as session:
        doc = session.get(ImportDocument, payload["document_id"])
        assert doc.file_name == "one.png"
        assert doc.status.value == "uploaded"


def test_pasted_manual_transaction_parses_code_and_amount_immediately(app_ctx):
    main, db, client = app_ctx
    from app.models import FundRule, FundType, ImportDocument, OcrRow, TransactionCandidate

    with Session(db.engine) as session:
        rule = session.get(FundRule, "021457") or FundRule(fund_code="021457")
        rule.fund_name = "易方达中证A500ETF联接A"
        rule.fund_type = FundType.open_fund
        session.add(rule)
        session.commit()

    response = client.post(
        "/manual-import",
        data={"manual_text": "2026-4-22 21:18 买入 021457 50.44"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db.engine) as session:
        doc = session.exec(select(ImportDocument)).one()
        row = session.exec(select(OcrRow)).one()
        candidate = session.exec(select(TransactionCandidate)).one()
        assert doc.status.value == "parsed"
        assert row.raw_text == "2026-4-22 21:18 买入 021457 50.44"
        assert candidate.fund_code == "021457"
        assert candidate.fund_name == "易方达中证A500ETF联接A"
        assert candidate.trade_date == date(2026, 4, 22)
        assert candidate.amount_cny == 50.44
        assert candidate.row_status == main.RowStatus.success


def test_upload_page_keeps_manual_template_visible(app_ctx):
    _, _, client = app_ctx

    response = client.get("/upload")

    assert response.status_code == 200
    assert 'action="/manual-import"' in response.text
    assert 'name="manual_text"' in response.text
    assert "买入：2026-04-22 21:18 买入 021457 50.44" in response.text
    assert "卖出：2026-04-22 21:18 卖出 021457 38.99" in response.text
    assert "placeholder=" not in response.text


def test_candidates_page_renders_candidate_form(app_ctx):
    main, db, client = app_ctx

    with Session(db.engine) as session:
        seed_open_fund(session, main)
        parse_rows(session, main, [["2024-01-02", "14:00:00", "成功", "易方达蓝筹", "买入", "金额1000元"]])

    response = client.get("/candidates")
    assert response.status_code == 200
    assert 'name="trade_date" value="2024-01-02"' in response.text
    assert 'name="amount_cny" value="1000.0"' in response.text


def test_incomplete_candidate_cannot_be_force_posted(app_ctx):
    main, db, client = app_ctx
    from app.models import CandidateIssue, FundTransaction, TransactionCandidate

    with Session(db.engine) as session:
        parse_rows(session, main, [["2024-01-02", "14:00:00", "成功", "未知基金", "买入", "金额1000元"]])
        candidate = session.exec(select(TransactionCandidate)).one()
        assert candidate.status.value == "needs_review"
        candidate_id = candidate.id

    response = client.post(f"/candidates/{candidate_id}/post", follow_redirects=False)
    assert response.status_code == 303

    with Session(db.engine) as session:
        assert session.exec(select(FundTransaction)).all() == []
        candidate = session.get(TransactionCandidate, candidate_id)
        assert candidate.status.value == "needs_review"
        assert "缺基金代码" in candidate.review_reason
        issues = session.exec(select(CandidateIssue).where(CandidateIssue.candidate_id == candidate_id)).all()
        assert {item.code for item in issues} >= {"missing_fund_code", "missing_fund_type"}


def test_masked_settings_do_not_overwrite_secrets(app_ctx):
    main, db, _ = app_ctx

    with Session(db.engine) as session:
        assert main.preserve_masked_secret("********", "real-secret") == "real-secret"
        assert main.preserve_masked_secret("已配置，末尾 cret", "real-secret") == "real-secret"
        assert main.preserve_masked_secret("未配置", "real-secret") == "real-secret"
        assert main.preserve_masked_secret("", "real-secret") == "real-secret"
        assert main.preserve_masked_secret("new-secret", "real-secret") == "new-secret"


def test_nav_sync_settings_are_saved(app_ctx):
    _, db, client = app_ctx
    from app.app_settings import runtime_settings

    response = client.post(
        "/settings/nav-sync",
        data={
            "nav_sync_enabled": "on",
            "nav_sync_time": "19:45",
            "nav_sync_pz": "800",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db.engine) as session:
        config = runtime_settings(session)
        assert config["NAV_SYNC_ENABLED"] == "true"
        assert config["NAV_SYNC_TIME"] == "19:45"
        assert config["NAV_SYNC_PZ"] == "800"


def test_ocr_and_nav_sync_settings_save_independently(app_ctx):
    _, db, client = app_ctx
    from app.app_settings import runtime_settings, save_settings

    with Session(db.engine) as session:
        save_settings(
            session,
            {
                "BAIDU_OCR_API_KEY": "secret-key",
                "BAIDU_OCR_SECRET_KEY": "secret-secret",
                "BAIDU_TABLE_OCR_ENDPOINT": "https://old.example/ocr",
                "NAV_SYNC_ENABLED": "true",
                "NAV_SYNC_TIME": "19:45",
                "NAV_SYNC_PZ": "800",
            },
        )

    client.post(
        "/settings",
        data={
            "ocr_enabled": "on",
            "baidu_ocr_api_key": "********",
            "baidu_ocr_secret_key": "********",
            "baidu_table_ocr_endpoint": "https://new.example/ocr",
        },
        follow_redirects=False,
    )
    with Session(db.engine) as session:
        config = runtime_settings(session)
        assert config["BAIDU_OCR_API_KEY"] == "secret-key"
        assert config["BAIDU_TABLE_OCR_ENDPOINT"] == "https://new.example/ocr"
        assert config["NAV_SYNC_ENABLED"] == "true"
        assert config["NAV_SYNC_TIME"] == "19:45"
        assert config["NAV_SYNC_PZ"] == "800"

    client.post(
        "/settings/nav-sync",
        data={
            "nav_sync_enabled": "on",
            "nav_sync_time": "20:15",
            "nav_sync_pz": "1200",
        },
        follow_redirects=False,
    )
    with Session(db.engine) as session:
        config = runtime_settings(session)
        assert config["BAIDU_OCR_API_KEY"] == "secret-key"
        assert config["BAIDU_TABLE_OCR_ENDPOINT"] == "https://new.example/ocr"
        assert config["NAV_SYNC_TIME"] == "20:15"
        assert config["NAV_SYNC_PZ"] == "1200"


def test_manual_nav_sync_updates_active_open_funds_only(app_ctx, monkeypatch):
    main, db, client = app_ctx
    from app.app_settings import runtime_settings
    from app.models import FundAlias, FundRule, FundTransaction, FundType, TransactionAction

    synced = []

    def fake_sync(session, code, pz=40000):
        synced.append((code, pz))
        return 2, None

    monkeypatch.setattr(main, "sync_nav_for_fund", fake_sync)
    with Session(db.engine) as session:
        seed_open_fund(session, main, code="005827", name="活跃基金")
        session.add(FundAlias(keyword="新基金", fund_code="006000", fund_name="新基金", fund_type=FundType.open_fund, source="test"))
        session.add(FundRule(fund_code="006000", fund_name="新基金", fund_type=FundType.open_fund))
        seed_open_fund(session, main, code="000001", name="已清仓基金")
        session.add(FundRule(fund_code="510300", fund_name="ETF", fund_type=FundType.etf))
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="活跃基金",
                fund_type=FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="006000",
                fund_name="新基金",
                fund_type=FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="000001",
                fund_name="已清仓基金",
                fund_type=FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="000001",
                fund_name="已清仓基金",
                fund_type=FundType.open_fund,
                trade_date=date(2024, 1, 3),
                action=TransactionAction.sell,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="510300",
                fund_name="ETF",
                fund_type=FundType.etf,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.commit()

    response = client.post("/settings/nav-sync-now", follow_redirects=False)
    assert response.status_code == 303
    assert synced == [("005827", 60), ("006000", 40000)]
    with Session(db.engine) as session:
        config = runtime_settings(session)
        assert config["NAV_SYNC_LAST_RUN_AT"]
        assert "成功 2 个" in config["NAV_SYNC_LAST_RESULT"]
        assert "增量 1 个，历史 1 个" in config["NAV_SYNC_LAST_RESULT"]


def test_daily_nav_sync_runs_once_after_shanghai_time(app_ctx):
    main, _, _ = app_ctx
    from datetime import datetime
    from zoneinfo import ZoneInfo

    config = {
        "NAV_SYNC_ENABLED": "true",
        "NAV_SYNC_TIME": "18:30",
        "NAV_SYNC_LAST_RUN_DATE": "2024-01-01",
    }
    before = datetime(2024, 1, 2, 18, 29, tzinfo=ZoneInfo("Asia/Shanghai"))
    after = datetime(2024, 1, 2, 18, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    already = {**config, "NAV_SYNC_LAST_RUN_DATE": "2024-01-02"}

    assert main.should_run_daily_nav_sync(before, config) is False
    assert main.should_run_daily_nav_sync(after, config) is True
    assert main.should_run_daily_nav_sync(after, already) is False


def test_nav_sync_uses_provider_fallback(app_ctx):
    _, db, _ = app_ctx
    from app.models import FundNav
    from app.nav import NavProvider, NavRow, sync_nav_for_fund

    class EmptyProvider(NavProvider):
        name = "empty"

        def fetch(self, fund_code: str, pz: int = 40000):
            return []

    class GoodProvider(NavProvider):
        name = "good"

        def fetch(self, fund_code: str, pz: int = 40000):
            return [NavRow(nav_date=date(2024, 1, 2), unit_nav=1.23, source=self.name)]

    with Session(db.engine) as session:
        inserted, error = sync_nav_for_fund(session, "005827", providers=[EmptyProvider(), GoodProvider()])
        assert error is None
        assert inserted == 1
        nav = session.exec(select(FundNav)).one()
        assert nav.source == "good"
        assert nav.unit_nav == 1.23

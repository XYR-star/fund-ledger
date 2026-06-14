from datetime import date
import importlib

from sqlmodel import Session, SQLModel


def fresh_app(tmp_path, monkeypatch):
    monkeypatch.setenv("FUND_LEDGER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FUND_LEDGER_DB", str(tmp_path / "fund-ledger.sqlite3"))

    import app.config
    import app.db
    import app.analysis
    import app.portfolio

    importlib.reload(app.config)
    importlib.reload(app.db)
    importlib.reload(app.portfolio)
    importlib.reload(app.analysis)
    SQLModel.metadata.drop_all(app.db.engine)
    SQLModel.metadata.create_all(app.db.engine)
    return app


def test_analysis_excludes_money_funds_from_return_contribution(tmp_path, monkeypatch):
    app = fresh_app(tmp_path, monkeypatch)

    from app.models import FundRule, FundTransaction, FundType, TransactionAction

    with Session(app.db.engine) as session:
        session.add(FundRule(fund_code="001010", fund_name="易方达增金宝货币A", fund_type=FundType.money_fund))
        session.add(FundRule(fund_code="005827", fund_name="易方达蓝筹精选混合", fund_type=FundType.open_fund))
        session.add(
            FundTransaction(
                fund_code="001010",
                fund_name="易方达增金宝货币A",
                fund_type=FundType.money_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=1000,
                share=1000,
                nav=1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="001010",
                fund_name="易方达增金宝货币A",
                fund_type=FundType.money_fund,
                trade_date=date(2024, 1, 3),
                action=TransactionAction.sell,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="005827",
                fund_name="易方达蓝筹精选混合",
                fund_type=FundType.open_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=100,
                share=100,
                nav=1,
            )
        )
        session.commit()

        positions = app.portfolio.calculate_position_summaries(session)
        report = app.analysis.build_analysis_report(session)

    assert [item.fund_code for item in positions] == ["005827"]
    reported_codes = {row["fund_code"] for row in report.contribution_winners + report.contribution_losers}
    assert "001010" not in reported_codes


def test_money_fund_without_eaccount_share_is_marked_as_transfer_vehicle(tmp_path, monkeypatch):
    app = fresh_app(tmp_path, monkeypatch)

    from app.models import EAccountImport, FundRule, FundTransaction, FundType, TransactionAction

    with Session(app.db.engine) as session:
        session.add(FundRule(fund_code="001010", fund_name="易方达增金宝货币A", fund_type=FundType.money_fund))
        session.add(EAccountImport(file_name="snapshot.csv", row_count=0))
        session.add(
            FundTransaction(
                fund_code="001010",
                fund_name="易方达增金宝货币A",
                fund_type=FundType.money_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=1000,
                share=1000,
                nav=1,
            )
        )
        session.add(
            FundTransaction(
                fund_code="001010",
                fund_name="易方达增金宝货币A",
                fund_type=FundType.money_fund,
                trade_date=date(2024, 1, 3),
                action=TransactionAction.sell,
                amount_cny=1000,
                share=1000,
                nav=1,
            )
        )
        session.commit()

        report = app.analysis.build_analysis_report(session)

    assert report.money_fund_rows == [
        {
            "fund_code": "001010",
            "fund_name": "易方达增金宝货币A",
            "role": "transfer_vehicle",
            "role_label": "中转通道",
            "local_share": 0.0,
            "official_share": None,
            "note": "最新 E 账户无份额，按申购费中转货币基金处理",
        }
    ]
    assert report.cash_flow_rows == []


def test_money_fund_with_eaccount_share_is_marked_as_cash_holding(tmp_path, monkeypatch):
    app = fresh_app(tmp_path, monkeypatch)

    from app.models import EAccountHolding, EAccountImport, FundRule, FundTransaction, FundType, TransactionAction

    with Session(app.db.engine) as session:
        session.add(FundRule(fund_code="001010", fund_name="易方达增金宝货币A", fund_type=FundType.money_fund))
        imported = EAccountImport(file_name="snapshot.csv", row_count=1)
        session.add(imported)
        session.flush()
        session.add(
            EAccountHolding(
                import_id=imported.id,
                fund_code="001010",
                fund_name="易方达增金宝货币A",
                official_share=500,
                status="matched",
            )
        )
        session.add(
            FundTransaction(
                fund_code="001010",
                fund_name="易方达增金宝货币A",
                fund_type=FundType.money_fund,
                trade_date=date(2024, 1, 2),
                action=TransactionAction.buy,
                amount_cny=500,
                share=500,
                nav=1,
            )
        )
        session.commit()

        report = app.analysis.build_analysis_report(session)

    assert report.money_fund_rows[0]["role"] == "cash_holding"
    assert report.money_fund_rows[0]["role_label"] == "真实持有"
    assert report.money_fund_rows[0]["official_share"] == 500
    assert report.money_fund_rows[0]["note"] == "后续接入万份收益后计算货币基金收益"
    assert report.cash_flow_rows[0]["buy"] == 500

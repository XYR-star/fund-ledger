from datetime import date
import importlib

from sqlmodel import Session, SQLModel


def test_analysis_excludes_money_funds_from_return_contribution(tmp_path, monkeypatch):
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

    from app.models import FundRule, FundTransaction, FundType, TransactionAction

    SQLModel.metadata.drop_all(app.db.engine)
    SQLModel.metadata.create_all(app.db.engine)
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

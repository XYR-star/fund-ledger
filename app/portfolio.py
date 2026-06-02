from dataclasses import dataclass
from datetime import date

from sqlmodel import Session, desc, select

from .models import FundNav, FundTransaction, TransactionAction


@dataclass
class Holding:
    fund_code: str
    fund_name: str
    share: float
    cost: float
    latest_nav: float | None
    nav_date: date | None
    market_value: float
    profit: float
    profit_rate: float | None


def calculate_holdings(session: Session) -> list[Holding]:
    txs = session.exec(select(FundTransaction).order_by(FundTransaction.trade_date)).all()
    grouped: dict[str, dict] = {}
    for tx in txs:
        item = grouped.setdefault(
            tx.fund_code,
            {"fund_name": tx.fund_name, "share": 0.0, "cost": 0.0},
        )
        if tx.fund_name:
            item["fund_name"] = tx.fund_name
        amount = tx.amount_cny or 0.0
        fee = tx.fee or 0.0
        share = tx.share
        if tx.action == TransactionAction.buy:
            if share is None and tx.nav:
                share = max((amount - fee) / tx.nav, 0)
            item["share"] += share or 0.0
            item["cost"] += amount + fee
        elif tx.action == TransactionAction.sell:
            sell_share = share or 0.0
            old_share = item["share"]
            if old_share > 0 and sell_share > 0:
                cost_reduction = item["cost"] * min(sell_share / old_share, 1)
                item["cost"] -= cost_reduction
            item["share"] -= sell_share
        elif tx.action == TransactionAction.dividend:
            item["cost"] -= amount
        elif tx.action == TransactionAction.dividend_reinvest:
            item["share"] += share or 0.0

    holdings: list[Holding] = []
    for fund_code, item in grouped.items():
        latest = session.exec(
            select(FundNav)
            .where(FundNav.fund_code == fund_code)
            .order_by(desc(FundNav.nav_date))
        ).first()
        latest_nav = latest.unit_nav if latest else None
        market_value = item["share"] * latest_nav if latest_nav else 0.0
        profit = market_value - item["cost"] if latest_nav else 0.0
        profit_rate = profit / item["cost"] if item["cost"] and latest_nav else None
        if abs(item["share"]) < 0.000001 and abs(item["cost"]) < 0.01:
            continue
        holdings.append(
            Holding(
                fund_code=fund_code,
                fund_name=item["fund_name"],
                share=item["share"],
                cost=item["cost"],
                latest_nav=latest_nav,
                nav_date=latest.nav_date if latest else None,
                market_value=market_value,
                profit=profit,
                profit_rate=profit_rate,
            )
        )
    return sorted(holdings, key=lambda h: h.market_value, reverse=True)


def xalpha_rows(session: Session) -> list[dict]:
    rows = []
    txs = session.exec(select(FundTransaction).order_by(FundTransaction.trade_date)).all()
    for tx in txs:
        trade = 0.0
        if tx.action == TransactionAction.buy:
            trade = tx.amount_cny or 0.0
        elif tx.action == TransactionAction.sell:
            trade = -(tx.share or 0.0)
        else:
            continue
        rows.append({"date": tx.trade_date.isoformat(), "fund": int(tx.fund_code), "trade": trade})
    return rows

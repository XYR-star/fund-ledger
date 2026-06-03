from dataclasses import dataclass
from datetime import date

from sqlmodel import Session, desc, select

from .models import FundNav, FundRule, FundTransaction, TransactionAction

EPS_SHARE = 0.000001
EPS_COST = 0.01


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


@dataclass
class PositionSummary(Holding):
    realized_profit: float
    realized_profit_rate: float | None
    total_buy_amount: float
    total_sell_amount: float
    last_trade_date: date | None
    is_closed: bool


def calculate_position_summaries(session: Session) -> list[PositionSummary]:
    txs = sorted(
        session.exec(select(FundTransaction).order_by(FundTransaction.trade_date, FundTransaction.id)).all(),
        key=lambda tx: (tx.trade_date, action_sort_key(tx.action), tx.id or 0),
    )
    money_codes = {
        rule.fund_code
        for rule in session.exec(select(FundRule)).all()
        if "货币" in (rule.fund_type or "")
    }
    grouped: dict[str, dict] = {}
    for tx in txs:
        item = grouped.setdefault(
            tx.fund_code,
            {
                "fund_name": tx.fund_name,
                "share": 0.0,
                "cost": 0.0,
                "realized_profit": 0.0,
                "total_buy_amount": 0.0,
                "total_sell_amount": 0.0,
                "last_trade_date": None,
            },
        )
        if tx.fund_name:
            item["fund_name"] = tx.fund_name
        item["last_trade_date"] = tx.trade_date
        amount = tx.amount_cny or 0.0
        fee = tx.fee or 0.0
        share = tx.share
        if tx.action == TransactionAction.buy:
            if share is None and tx.nav:
                share = max((amount - fee) / tx.nav, 0)
            item["share"] += share or 0.0
            item["cost"] += amount + fee
            item["total_buy_amount"] += amount + fee
        elif tx.action == TransactionAction.sell:
            sell_share = share or 0.0
            old_share = item["share"]
            cost_reduction = 0.0
            if old_share > 0 and sell_share > 0:
                cost_reduction = item["cost"] * min(sell_share / old_share, 1)
                item["cost"] -= cost_reduction
            proceeds = amount or ((sell_share * tx.nav) if tx.nav else 0.0)
            net_proceeds = max(proceeds - fee, 0.0)
            item["total_sell_amount"] += net_proceeds
            item["realized_profit"] += net_proceeds - cost_reduction
            item["share"] -= sell_share
        elif tx.action == TransactionAction.dividend:
            item["cost"] -= amount
            item["realized_profit"] += amount
        elif tx.action == TransactionAction.dividend_reinvest:
            item["share"] += share or 0.0

    positions: list[PositionSummary] = []
    for fund_code, item in grouped.items():
        if fund_code in money_codes:
            latest = session.exec(
                select(FundNav)
                .where(FundNav.fund_code == fund_code)
                .order_by(desc(FundNav.nav_date))
            ).first()
            latest_nav = 1.0
        else:
            latest = session.exec(
                select(FundNav)
                .where(FundNav.fund_code == fund_code)
                .order_by(desc(FundNav.nav_date))
            ).first()
            latest_nav = latest.unit_nav if latest else None
        market_value = item["share"] * latest_nav if latest_nav else 0.0
        profit = market_value - item["cost"] if latest_nav else 0.0
        profit_rate = profit / item["cost"] if item["cost"] and latest_nav else None
        is_closed = abs(item["share"]) < EPS_SHARE and abs(item["cost"]) < EPS_COST
        realized_profit_rate = (
            item["realized_profit"] / item["total_buy_amount"]
            if item["total_buy_amount"]
            else None
        )
        positions.append(
            PositionSummary(
                fund_code=fund_code,
                fund_name=item["fund_name"],
                share=item["share"],
                cost=item["cost"],
                latest_nav=latest_nav,
                nav_date=latest.nav_date if latest else None,
                market_value=market_value,
                profit=profit,
                profit_rate=profit_rate,
                realized_profit=item["realized_profit"],
                realized_profit_rate=realized_profit_rate,
                total_buy_amount=item["total_buy_amount"],
                total_sell_amount=item["total_sell_amount"],
                last_trade_date=item["last_trade_date"],
                is_closed=is_closed,
            )
        )
    return sorted(positions, key=lambda h: (h.is_closed, -h.market_value, h.fund_code))


def calculate_holdings(session: Session) -> list[Holding]:
    holdings: list[Holding] = []
    for item in calculate_position_summaries(session):
        if item.is_closed:
            continue
        holdings.append(
            Holding(
                fund_code=item.fund_code,
                fund_name=item.fund_name,
                share=item.share,
                cost=item.cost,
                latest_nav=item.latest_nav,
                nav_date=item.nav_date,
                market_value=item.market_value,
                profit=item.profit,
                profit_rate=item.profit_rate,
            )
        )
    return sorted(holdings, key=lambda h: h.market_value, reverse=True)


def action_sort_key(action: TransactionAction) -> int:
    if action in {TransactionAction.buy, TransactionAction.dividend_reinvest}:
        return 0
    if action == TransactionAction.dividend:
        return 1
    if action == TransactionAction.sell:
        return 2
    return 3


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

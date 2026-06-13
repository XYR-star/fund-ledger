from collections import defaultdict
from dataclasses import dataclass

from sqlmodel import Session, desc, select

from .models import (
    CandidateStatus,
    EAccountHolding,
    EAccountImport,
    FundRule,
    FundTransaction,
    FundType,
    ImportDocument,
    ImportStatus,
    TransactionAction,
    TransactionCandidate,
)
from .portfolio import calculate_position_summaries


@dataclass
class AnalysisItem:
    label: str
    value: str
    detail: str = ""
    tone: str = "info"


@dataclass
class AnalysisReport:
    health: list[AnalysisItem]
    returns: list[AnalysisItem]
    contribution_winners: list[dict]
    contribution_losers: list[dict]
    platform_rows: list[dict]
    cash_flow_rows: list[dict]
    pending_items: list[AnalysisItem]


def build_analysis_report(session: Session) -> AnalysisReport:
    positions = calculate_position_summaries(session)
    active = [item for item in positions if not item.is_closed]
    latest_import = session.exec(select(EAccountImport).order_by(desc(EAccountImport.imported_at), desc(EAccountImport.id))).first()
    latest_eaccount_issues = []
    if latest_import and latest_import.id:
        latest_eaccount_issues = session.exec(
            select(EAccountHolding).where(
                EAccountHolding.import_id == latest_import.id,
                EAccountHolding.status.in_(["mismatch", "missing"]),
            )
        ).all()

    needs_review = session.exec(select(TransactionCandidate).where(TransactionCandidate.status == CandidateStatus.needs_review)).all()
    corrected_count = len(session.exec(select(TransactionCandidate).where(TransactionCandidate.manual_corrected == True)).all())  # noqa: E712
    errored_imports = session.exec(select(ImportDocument).where(ImportDocument.status == ImportStatus.error)).all()
    unknown_rules = session.exec(select(FundRule).where(FundRule.fund_type == FundType.unknown)).all()
    missing_nav_positions = [item for item in active if item.latest_nav is None]

    health = [
        AnalysisItem("E 账户差异", str(len(latest_eaccount_issues)), latest_import.file_name if latest_import else "尚未导入基金 E 账户", _tone_count(len(latest_eaccount_issues))),
        AnalysisItem("待修正候选", str(len(needs_review)), "需要人工补字段或确认", _tone_count(len(needs_review))),
        AnalysisItem("已修正候选", str(corrected_count), "人工改过的候选/流水", "warn" if corrected_count else "info"),
        AnalysisItem("导入失败", str(len(errored_imports)), "OCR 或解析失败的导入", _tone_count(len(errored_imports))),
        AnalysisItem("未知基金类型", str(len(unknown_rules)), "不会自动进入收益计算", _tone_count(len(unknown_rules))),
        AnalysisItem("缺最新净值", str(len(missing_nav_positions)), "影响市值和收益", _tone_count(len(missing_nav_positions))),
    ]

    market_value = sum(item.market_value for item in active)
    cost = sum(item.cost for item in active)
    holding_profit = sum(item.profit for item in active)
    realized = sum(item.realized_profit for item in positions)
    total_profit = holding_profit + realized
    total_buy = sum(item.total_buy_amount for item in positions)
    total_sell = sum(item.total_sell_amount for item in positions)
    return_rate = total_profit / total_buy if total_buy else None
    returns = [
        AnalysisItem("最新市值", _money(market_value), f"活跃持仓 {len(active)} 只"),
        AnalysisItem("持仓成本", _money(cost), "未清仓部分成本"),
        AnalysisItem("持仓收益", _money(holding_profit), "当前浮动收益", _tone_money(holding_profit)),
        AnalysisItem("已实现收益", _money(realized), "卖出后落袋收益", _tone_money(realized)),
        AnalysisItem("累计收益", _money(total_profit), f"收益率 {_percent(return_rate)}", _tone_money(total_profit)),
        AnalysisItem("净投入", _money(total_buy - total_sell), f"买入 {_money(total_buy)} / 卖出 {_money(total_sell)}"),
    ]

    contribution_rows = [
        {
            "fund_code": item.fund_code,
            "fund_name": item.fund_name,
            "market_value": item.market_value,
            "profit": item.profit,
            "realized_profit": item.realized_profit,
            "total_profit": item.profit + item.realized_profit,
            "profit_rate": item.profit_rate,
        }
        for item in positions
    ]
    winners = sorted(contribution_rows, key=lambda item: item["total_profit"], reverse=True)[:5]
    losers = sorted(contribution_rows, key=lambda item: item["total_profit"])[:5]

    platform_rows = _platform_rows(active)
    cash_flow_rows = _cash_flow_rows(session)
    pending_items = _pending_items(latest_eaccount_issues, needs_review, errored_imports, unknown_rules, missing_nav_positions)
    return AnalysisReport(health, returns, winners, losers, platform_rows, cash_flow_rows, pending_items)


def _platform_rows(active) -> list[dict]:
    grouped = defaultdict(lambda: {"platform": "", "fund_count": 0, "market_value": 0.0, "profit": 0.0, "total_profit": 0.0})
    for item in active:
        key = item.platform or "未分类"
        row = grouped[key]
        row["platform"] = key
        row["fund_count"] += 1
        row["market_value"] += item.market_value
        row["profit"] += item.profit
        row["total_profit"] += item.profit + item.realized_profit
    return sorted(grouped.values(), key=lambda item: item["market_value"], reverse=True)


def _cash_flow_rows(session: Session) -> list[dict]:
    rows: dict[str, dict] = defaultdict(lambda: {"month": "", "buy": 0.0, "sell": 0.0, "dividend": 0.0, "reinvest": 0.0, "net": 0.0})
    txs = session.exec(select(FundTransaction).order_by(FundTransaction.trade_date)).all()
    for tx in txs:
        month = tx.trade_date.strftime("%Y-%m")
        row = rows[month]
        row["month"] = month
        if tx.action == TransactionAction.buy:
            row["buy"] += tx.amount_cny or 0.0
        elif tx.action == TransactionAction.sell:
            row["sell"] += tx.amount_cny or 0.0
        elif tx.action == TransactionAction.dividend:
            row["dividend"] += tx.amount_cny or 0.0
        elif tx.action == TransactionAction.dividend_reinvest:
            row["reinvest"] += tx.amount_cny or 0.0
        row["net"] = row["buy"] - row["sell"]
    return sorted(rows.values(), key=lambda item: item["month"], reverse=True)[:12]


def _pending_items(eaccount_issues, needs_review, errored_imports, unknown_rules, missing_nav_positions) -> list[AnalysisItem]:
    items: list[AnalysisItem] = []
    for row in eaccount_issues[:5]:
        items.append(AnalysisItem("对账差异", f"{row.fund_name} {row.fund_code}", row.issue_summary, "danger"))
    for candidate in needs_review[:5]:
        items.append(AnalysisItem("候选待修正", f"#{candidate.id}", candidate.review_reason or candidate.raw_text[:60], "warn"))
    for doc in errored_imports[:5]:
        items.append(AnalysisItem("导入失败", f"#{doc.id} {doc.file_name or ''}", doc.error_message[:80], "danger"))
    for rule in unknown_rules[:5]:
        items.append(AnalysisItem("未知类型", f"{rule.fund_name} {rule.fund_code}", "请在基金规则中确认类型", "warn"))
    for position in missing_nav_positions[:5]:
        items.append(AnalysisItem("缺净值", f"{position.fund_name} {position.fund_code}", "同步净值后收益才准确", "warn"))
    return items


def _tone_count(value: int) -> str:
    return "danger" if value else "info"


def _tone_money(value: float) -> str:
    if value < 0:
        return "danger"
    return "info"


def _money(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.2f}%"

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlmodel import Session, select

from .models import BenchmarkNav, FundNav, FundRule, FundTransaction, TransactionAction
from .portfolio import calculate_position_summaries

HS300_CODE = "000300"
HS300_NAME = "沪深300"


@dataclass
class ChartPoint:
    date: date
    value: float


@dataclass
class TradeMarker:
    date: date
    value: float
    action: str
    amount: float
    share: float


@dataclass
class FundPerformanceChart:
    fund_code: str
    fund_name: str
    latest_return: float | None
    benchmark_return: float | None
    excess_return: float | None
    fund_points: list[ChartPoint]
    benchmark_points: list[ChartPoint]
    trade_markers: list[TradeMarker]
    svg_path: str
    svg_area: str
    benchmark_path: str
    benchmark_area: str
    marker_positions: list[dict[str, Any]]
    y_ticks: list[dict[str, Any]]
    x_ticks: list[dict[str, Any]]
    start_date: date | None
    end_date: date | None


def build_performance_charts(
    session: Session,
    include_closed: bool = False,
    fund_code: str | None = None,
) -> list[FundPerformanceChart]:
    txs = session.exec(select(FundTransaction).order_by(FundTransaction.trade_date, FundTransaction.id)).all()
    if fund_code:
        funds = [fund_code]
    else:
        funds = sorted({tx.fund_code for tx in txs})
    if not include_closed and not fund_code:
        active_codes = {
            item.fund_code
            for item in calculate_position_summaries(session)
            if not item.is_closed
        }
        funds = [code for code in funds if code in active_codes]
    start_dates = {
        code: min(tx.trade_date for tx in txs if tx.fund_code == code)
        for code in funds
        if any(tx.fund_code == code for tx in txs)
    }
    navs_by_fund = _navs_for_funds(session, funds, min(start_dates.values()) if start_dates else None)
    charts = []
    for fund_code in funds:
        fund_txs = [tx for tx in txs if tx.fund_code == fund_code]
        if not fund_txs:
            continue
        fund_name = next((t.fund_name for t in reversed(fund_txs) if t.fund_name), "")
        navs = navs_by_fund.get(fund_code, [])
        if len(navs) < 2:
            continue
        start_date = start_dates[fund_code]
        fund_points = _normalize_nav_points(
            [(_nav_date(n), _nav_value(n)) for n in navs if _nav_date(n) >= start_date]
        )
        if len(fund_points) < 2:
            continue
        benchmark_points = _benchmark_points_for_range(session, fund_points[0].date, fund_points[-1].date)
        trade_markers = _trade_markers(fund_txs, fund_points)
        latest_return = fund_points[-1].value
        benchmark_return = benchmark_points[-1].value if benchmark_points else None
        values = [p.value for p in fund_points + benchmark_points]
        for m in trade_markers:
            values.append(m.value)
        y_min, y_max = _padded_range(values)
        charts.append(
            FundPerformanceChart(
                fund_code=fund_code,
                fund_name=fund_name,
                latest_return=latest_return,
                benchmark_return=benchmark_return,
                excess_return=latest_return - benchmark_return if latest_return is not None and benchmark_return is not None else None,
                fund_points=fund_points,
                benchmark_points=benchmark_points,
                trade_markers=trade_markers,
                svg_path=_svg_path(fund_points, y_min, y_max),
                svg_area=_svg_area_path(fund_points, y_min, y_max),
                benchmark_path=_svg_path(benchmark_points, y_min, y_max),
                benchmark_area=_svg_area_path(benchmark_points, y_min, y_max),
                marker_positions=_marker_positions(trade_markers, fund_points[0].date, fund_points[-1].date, y_min, y_max),
                y_ticks=_y_ticks(y_min, y_max),
                x_ticks=_x_ticks(fund_points[0].date, fund_points[-1].date),
                start_date=fund_points[0].date,
                end_date=fund_points[-1].date,
            )
        )
    return sorted(charts, key=lambda c: abs(c.latest_return or 0), reverse=True)


def build_aggregate_charts(session: Session) -> list[FundPerformanceChart]:
    txs = session.exec(select(FundTransaction).order_by(FundTransaction.trade_date, FundTransaction.id)).all()
    rules = {r.fund_code: r for r in session.exec(select(FundRule)).all()}
    active_codes = {
        item.fund_code
        for item in calculate_position_summaries(session)
        if not item.is_closed
    }
    if not active_codes:
        return []

    min_date = min(tx.trade_date for tx in txs if tx.fund_code in active_codes)
    all_navs = {}
    for code in active_codes:
        navs = session.exec(
            select(FundNav).where(FundNav.fund_code == code, FundNav.nav_date >= min_date).order_by(FundNav.nav_date)
        ).all()
        if navs:
            all_navs[code] = navs

    overall_points = _aggregate_value_over_time(session, active_codes, all_navs)

    by_platform: dict[str, set[str]] = {}
    for code in active_codes:
        rule = rules.get(code)
        pf = rule.platform if rule and rule.platform else "未分类"
        by_platform.setdefault(pf, set()).add(code)

    charts: list[FundPerformanceChart] = []

    if overall_points and len(overall_points) >= 2:
        positions = calculate_position_summaries(session)
        active = [p for p in positions if not p.is_closed]
        total_cost = sum(p.cost for p in active)
        latest_val = overall_points[-1].value
        total_return = (latest_val - total_cost) / total_cost if total_cost > 0 else 0
        return_points = _value_points_to_return_points(overall_points, total_cost)
        y_min, y_max = _padded_range([p.value for p in return_points])
        charts.append(FundPerformanceChart(
            fund_code="TOTAL", fund_name="总体持仓收益",
            latest_return=total_return,
            benchmark_return=None, excess_return=None,
            fund_points=return_points, benchmark_points=[],
            trade_markers=[], svg_path=_svg_path(return_points, y_min, y_max),
            svg_area=_svg_area_path(return_points, y_min, y_max),
            benchmark_path="", benchmark_area="",
            marker_positions=[], y_ticks=_y_ticks(y_min, y_max),
            x_ticks=_x_ticks(return_points[0].date, return_points[-1].date),
            start_date=return_points[0].date, end_date=return_points[-1].date,
        ))

    for pf_name, pf_codes in sorted(by_platform.items()):
        pf_points = _aggregate_value_over_time(session, pf_codes, all_navs)
        if not pf_points or len(pf_points) < 2:
            continue
        active_positions = [p for p in calculate_position_summaries(session) if p.fund_code in pf_codes and not p.is_closed]
        pf_cost = sum(p.cost for p in active_positions)
        pf_ret = (pf_points[-1].value - pf_cost) / pf_cost if pf_cost > 0 else 0
        pf_return_points = _value_points_to_return_points(pf_points, pf_cost)
        ymin, ymax = _padded_range([p.value for p in pf_return_points])
        charts.append(FundPerformanceChart(
            fund_code=pf_name, fund_name=pf_name,
            latest_return=pf_ret,
            benchmark_return=None, excess_return=None,
            fund_points=pf_return_points, benchmark_points=[],
            trade_markers=[], svg_path=_svg_path(pf_return_points, ymin, ymax),
            svg_area=_svg_area_path(pf_return_points, ymin, ymax),
            benchmark_path="", benchmark_area="",
            marker_positions=[], y_ticks=_y_ticks(ymin, ymax),
            x_ticks=_x_ticks(pf_return_points[0].date, pf_return_points[-1].date),
            start_date=pf_return_points[0].date, end_date=pf_return_points[-1].date,
        ))

    return charts


def _nav_date(nav: FundNav) -> date:
    return nav.nav_date


def _nav_value(nav: FundNav) -> float:
    return nav.accumulated_nav if nav.accumulated_nav and nav.accumulated_nav > 0 else nav.unit_nav


def _navs_for_funds(session: Session, codes: list[str], start: date | None) -> dict[str, list[FundNav]]:
    if not codes:
        return {}
    query = select(FundNav).where(FundNav.fund_code.in_(codes))
    if start:
        query = query.where(FundNav.nav_date >= start)
    navs = session.exec(query.order_by(FundNav.fund_code, FundNav.nav_date)).all()
    result: dict[str, list[FundNav]] = {}
    for n in navs:
        result.setdefault(n.fund_code, []).append(n)
    return result


def _normalize_nav_points(items: list[tuple[date, float]]) -> list[ChartPoint]:
    clean = [(d, v) for d, v in items if v and v > 0]
    if not clean:
        return []
    base = clean[0][1]
    return [ChartPoint(d, v / base - 1) for d, v in clean]


def _normalize_points(points: list[ChartPoint]) -> list[ChartPoint]:
    if not points:
        return []
    base = points[0].value
    return [ChartPoint(p.date, p.value / base - 1) for p in points]


def _benchmark_points_for_range(session: Session, start: date, end: date) -> list[ChartPoint]:
    items = session.exec(
        select(BenchmarkNav)
        .where(BenchmarkNav.benchmark_code == HS300_CODE, BenchmarkNav.nav_date >= start, BenchmarkNav.nav_date <= end)
        .order_by(BenchmarkNav.nav_date)
    ).all()
    return _normalize_nav_points([(item.nav_date, item.close_value) for item in items])


def _trade_markers(txs: list[FundTransaction], points: list[ChartPoint]) -> list[TradeMarker]:
    markers = []
    for tx in txs:
        if tx.action not in {TransactionAction.buy, TransactionAction.sell, TransactionAction.dividend_reinvest}:
            continue
        point = next((p for p in points if p.date >= tx.trade_date), None)
        if not point:
            continue
        action = "sell" if tx.action == TransactionAction.sell else "buy"
        markers.append(TradeMarker(date=point.date, value=point.value, action=action, amount=tx.amount_cny or 0, share=tx.share or 0))
    return markers


def _aggregate_value_over_time(session: Session, codes: set[str], all_navs: dict[str, list[FundNav]]) -> list[ChartPoint]:
    txs = session.exec(
        select(FundTransaction).where(FundTransaction.fund_code.in_(codes)).order_by(FundTransaction.trade_date, FundTransaction.id)
    ).all()
    date_values: dict[date, float] = {}
    for code in codes:
        navs = all_navs.get(code, [])
        shares_over_time = _shares_over_time(
            [t for t in txs if t.fund_code == code], [n.nav_date for n in navs]
        )
        for n in navs:
            shares = shares_over_time.get(n.nav_date, 0)
            val = shares * n.unit_nav
            date_values[n.nav_date] = date_values.get(n.nav_date, 0) + val
    if not date_values:
        return []
    sorted_dates = sorted(date_values.keys())
    points = [ChartPoint(d, date_values[d]) for d in sorted_dates if date_values[d] > 10]
    return points if len(points) >= 2 else []


def _value_points_to_return_points(points: list[ChartPoint], cost: float) -> list[ChartPoint]:
    if cost <= 0:
        return [ChartPoint(point.date, 0) for point in points]
    return [ChartPoint(point.date, point.value / cost - 1) for point in points]


def _shares_over_time(fund_txs: list[FundTransaction], nav_dates: list[date]) -> dict[date, float]:
    nav_set = set(nav_dates)
    txs_sorted = sorted(fund_txs, key=lambda t: (t.trade_date, 0 if t.action in {TransactionAction.buy, TransactionAction.dividend_reinvest} else 1, t.id or 0))
    result: dict[date, float] = {}
    shares = 0.0
    tx_idx = 0
    for d in sorted(nav_set):
        while tx_idx < len(txs_sorted) and txs_sorted[tx_idx].trade_date <= d:
            tx = txs_sorted[tx_idx]
            if tx.action == TransactionAction.buy:
                shares += tx.share or 0
            elif tx.action == TransactionAction.sell:
                shares -= tx.share or 0
            elif tx.action == TransactionAction.dividend_reinvest:
                shares += tx.share or 0
            tx_idx += 1
        result[d] = shares
    return result


def _padded_range(values: list[float]) -> tuple[float, float]:
    if not values:
        return -0.1, 0.1
    low = min(values)
    high = max(values)
    if low == high:
        low -= 0.05
        high += 0.05
    padding = max((high - low) * 0.12, 0.02)
    return low - padding, high + padding


def _svg_path(points: list[ChartPoint], y_min: float, y_max: float) -> str:
    if len(points) < 2:
        return ""
    start = points[0].date
    end = points[-1].date
    coords = [_point_to_svg(p.date, p.value, start, end, y_min, y_max) for p in points]
    return " ".join(("M" if i == 0 else "L") + f"{x:.2f},{y:.2f}" for i, (x, y) in enumerate(coords))


def _svg_area_path(points: list[ChartPoint], y_min: float, y_max: float) -> str:
    if len(points) < 2:
        return ""
    start = points[0].date
    end = points[-1].date
    coords = [_point_to_svg(p.date, p.value, start, end, y_min, y_max) for p in points]
    parts = [f"M{coords[0][0]:.2f},{coords[0][1]:.2f}"]
    for x, y in coords:
        parts.append(f"L{x:.2f},{y:.2f}")
    parts.append(f"L{coords[-1][0]:.2f},100 L{coords[0][0]:.2f},100 Z")
    return " ".join(parts)


def _marker_positions(markers: list[TradeMarker], start: date, end: date, y_min: float, y_max: float) -> list[dict]:
    result = []
    for m in markers:
        x, y = _point_to_svg(m.date, m.value, start, end, y_min, y_max)
        result.append({"x": x, "y": y, "date": str(m.date), "action": m.action, "amount": m.amount, "share": m.share})
    return result


def _point_to_svg(d: date, value: float, start: date, end: date, y_min: float, y_max: float) -> tuple[float, float]:
    total_days = max((end - start).days, 1)
    x = ((d - start).days / total_days) * 100
    y = 100 - ((value - y_min) / (y_max - y_min)) * 100
    return max(0, min(100, x)), max(0, min(100, y))


def _y_ticks(y_min: float, y_max: float) -> list[dict]:
    ticks = []
    for ratio in (0.0, 0.5, 1.0):
        value = y_max - (y_max - y_min) * ratio
        ticks.append({"y": ratio * 100, "label": _format_return(value)})
    return ticks


def _value_ticks(y_min: float, y_max: float) -> list[dict]:
    ticks = []
    for ratio in (0.0, 0.5, 1.0):
        value = y_max - (y_max - y_min) * ratio
        if abs(value) >= 10000:
            label = f"¥{value/10000:.1f}万"
        else:
            label = f"¥{value:.0f}"
        ticks.append({"y": ratio * 100, "label": label})
    return ticks


def _x_ticks(start: date, end: date) -> list[dict]:
    ticks = []
    if start >= end:
        return ticks
    from calendar import monthrange
    total_days = max((end - start).days, 1)
    y = start.year
    m = start.month
    while True:
        d = date(y, m, 1)
        if d > end:
            break
        if d >= start:
            x = (d - start).days / total_days * 100
            ticks.append({"x": x, "label": f"{m}月"})
        m += 1
        if m > 12:
            m = 1
            y += 1
    return ticks


def _format_return(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def sync_hs300(session: Session) -> tuple[int, str | None]:
    rows, source, error = _fetch_hs300()
    if error:
        return 0, error
    if not rows:
        return 0, "empty benchmark response"
    inserted = 0
    for row in rows:
        nav_date = _parse_date(row.get("date") or row.get("日期") or row.get("day"))
        close_value = _parse_float(row.get("close") or row.get("收盘"))
        if nav_date is None or close_value is None:
            continue
        existing = session.exec(
            select(BenchmarkNav).where(BenchmarkNav.benchmark_code == HS300_CODE, BenchmarkNav.nav_date == nav_date)
        ).first()
        if existing:
            existing.close_value = close_value
            existing.source = source
            existing.updated_at = datetime.utcnow()
            session.add(existing)
        else:
            session.add(BenchmarkNav(
                benchmark_code=HS300_CODE, benchmark_name=HS300_NAME,
                nav_date=nav_date, close_value=close_value, source=source,
            ))
            inserted += 1
    session.commit()
    return inserted, None


def _fetch_hs300() -> tuple[list[dict], str, str | None]:
    errors = []
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily_em(symbol="sh000300")
    except Exception as exc:
        errors.append(f"akshare: {exc}")
    else:
        if df is not None and not df.empty:
            return list(df.to_dict(orient="records")), "akshare:index_daily_em", None
        errors.append("akshare: empty benchmark response")
    try:
        import requests
        resp = requests.get(
            "https://quotes.sina.cn/cn/api/jsonp.php/var%20_sh000300_=/CN_MarketDataService.getKLineData",
            params={"symbol": "sh000300", "scale": "240", "ma": "no", "datalen": "1500"},
            timeout=30,
        )
        resp.raise_for_status()
        match = re.search(r"\((\[.*\])\)", resp.text, re.S)
        if not match:
            raise RuntimeError("unexpected sina response")
        rows = json.loads(match.group(1))
        if rows:
            return rows, "sina:kline", None
        errors.append("sina: empty benchmark response")
    except Exception as exc:
        errors.append(f"sina: {exc}")
    return [], "", "; ".join(errors)


def _parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(str(value)[:10].replace("/", "-"), fmt).date()
        except ValueError:
            continue
    return None


def _parse_float(value) -> float | None:
    if value in (None, "", "--"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None

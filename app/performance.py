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
    start_date: date | None
    end_date: date | None

    @property
    def buy_markers(self) -> list[TradeMarker]:
        return [marker for marker in self.trade_markers if marker.action == "buy"]


def build_performance_charts(
    session: Session,
    include_closed: bool = False,
    fund_code: str | None = None,
) -> list[FundPerformanceChart]:
    txs = session.exec(select(FundTransaction).order_by(FundTransaction.trade_date, FundTransaction.id)).all()
    money_codes = {
        rule.fund_code
        for rule in session.exec(select(FundRule)).all()
        if "货币" in (rule.fund_type or "")
    }
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
    navs_by_fund = navs_for_funds(session, funds, min(start_dates.values()) if start_dates else None)
    charts = []
    for fund_code in funds:
        fund_txs = [tx for tx in txs if tx.fund_code == fund_code]
        if not fund_txs:
            continue
        fund_name = next((tx.fund_name for tx in reversed(fund_txs) if tx.fund_name), "")
        navs = navs_by_fund.get(fund_code, [])
        if len(navs) < 2:
            continue
        start_date = start_dates[fund_code]
        fund_points = normalize_nav_points(
            [
                (item.nav_date, 1.0 if fund_code in money_codes else (item.accumulated_nav if item.accumulated_nav and item.accumulated_nav > 0 else item.unit_nav))
                for item in navs
                if item.nav_date >= start_date
            ]
        )
        if len(fund_points) < 2:
            continue
        benchmark_points = benchmark_points_for_range(session, fund_points[0].date, fund_points[-1].date)
        trade_markers = trade_markers_for_transactions(fund_txs, fund_points)
        latest_return = fund_points[-1].value if fund_points else None
        benchmark_return = benchmark_points[-1].value if benchmark_points else None
        values = [point.value for point in fund_points + benchmark_points]
        for marker in trade_markers:
            values.append(marker.value)
        y_min, y_max = padded_range(values)
        charts.append(
            FundPerformanceChart(
                fund_code=fund_code,
                fund_name=fund_name,
                latest_return=latest_return,
                benchmark_return=benchmark_return,
                excess_return=(
                    latest_return - benchmark_return
                    if latest_return is not None and benchmark_return is not None
                    else None
                ),
                fund_points=fund_points,
                benchmark_points=benchmark_points,
                trade_markers=trade_markers,
                svg_path=svg_path(fund_points, y_min, y_max),
                svg_area=svg_area_path(fund_points, y_min, y_max),
                benchmark_path=svg_path(benchmark_points, y_min, y_max),
                benchmark_area=svg_area_path(benchmark_points, y_min, y_max),
                marker_positions=marker_positions(trade_markers, fund_points[0].date, fund_points[-1].date, y_min, y_max),
                y_ticks=y_ticks(y_min, y_max),
                start_date=fund_points[0].date,
                end_date=fund_points[-1].date,
            )
        )
    return charts


def navs_for_funds(session: Session, fund_codes: list[str], start_date: date | None = None) -> dict[str, list[FundNav]]:
    if not fund_codes:
        return {}
    query = select(FundNav).where(FundNav.fund_code.in_(fund_codes))
    if start_date:
        query = query.where(FundNav.nav_date >= start_date)
    navs = session.exec(
        query.order_by(FundNav.fund_code, FundNav.nav_date)
    ).all()
    result: dict[str, list[FundNav]] = {}
    for nav in navs:
        result.setdefault(nav.fund_code, []).append(nav)
    return result


def normalize_nav_points(items: list[tuple[date, float]]) -> list[ChartPoint]:
    clean = [(item_date, value) for item_date, value in items if value and value > 0]
    if not clean:
        return []
    base = clean[0][1]
    return [ChartPoint(item_date, value / base - 1) for item_date, value in clean]


def benchmark_points_for_range(session: Session, start_date: date, end_date: date) -> list[ChartPoint]:
    items = session.exec(
        select(BenchmarkNav)
        .where(
            BenchmarkNav.benchmark_code == HS300_CODE,
            BenchmarkNav.nav_date >= start_date,
            BenchmarkNav.nav_date <= end_date,
        )
        .order_by(BenchmarkNav.nav_date)
    ).all()
    return normalize_nav_points([(item.nav_date, item.close_value) for item in items])


def trade_markers_for_transactions(
    txs: list[FundTransaction],
    fund_points: list[ChartPoint],
) -> list[TradeMarker]:
    markers = []
    for tx in txs:
        if tx.action not in {TransactionAction.buy, TransactionAction.sell, TransactionAction.dividend_reinvest}:
            continue
        point = nearest_point_on_or_after(fund_points, tx.trade_date)
        if not point:
            continue
        action = "sell" if tx.action == TransactionAction.sell else "buy"
        markers.append(
            TradeMarker(
                date=point.date,
                value=point.value,
                action=action,
                amount=tx.amount_cny or 0.0,
                share=tx.share or 0.0,
            )
        )
    return markers


def nearest_point_on_or_after(points: list[ChartPoint], target: date) -> ChartPoint | None:
    for point in points:
        if point.date >= target:
            return point
    return None


def sync_hs300(session: Session) -> tuple[int, str | None]:
    rows, source, error = fetch_hs300_rows()
    if error:
        return 0, error
    if not rows:
        return 0, "empty benchmark response"
    inserted = 0
    for row in rows:
        nav_date = parse_date(row.get("date") or row.get("日期") or row.get("day"))
        close_value = parse_float(row.get("close") or row.get("收盘"))
        if nav_date is None or close_value is None:
            continue
        existing = session.exec(
            select(BenchmarkNav).where(
                BenchmarkNav.benchmark_code == HS300_CODE,
                BenchmarkNav.nav_date == nav_date,
            )
        ).first()
        if existing:
            existing.close_value = close_value
            existing.source = source
            existing.updated_at = datetime.utcnow()
            session.add(existing)
        else:
            session.add(
                BenchmarkNav(
                    benchmark_code=HS300_CODE,
                    benchmark_name=HS300_NAME,
                    nav_date=nav_date,
                    close_value=close_value,
                    source=source,
                )
            )
            inserted += 1
    session.commit()
    return inserted, None


def fetch_hs300_rows() -> tuple[list[dict[str, Any]], str, str | None]:
    errors = []
    try:
        import akshare as ak

        df = ak.stock_zh_index_daily_em(symbol="sh000300")
    except Exception as exc:  # pragma: no cover - network/source dependent
        errors.append(f"akshare: {exc}")
    else:
        if df is not None and not df.empty:
            return list(df.to_dict(orient="records")), "akshare:index_daily_em", None
        errors.append("akshare: empty benchmark response")

    try:
        import requests

        response = requests.get(
            "https://quotes.sina.cn/cn/api/jsonp.php/var%20_sh000300_=/"
            "CN_MarketDataService.getKLineData",
            params={"symbol": "sh000300", "scale": "240", "ma": "no", "datalen": "1500"},
            timeout=30,
        )
        response.raise_for_status()
        match = re.search(r"\((\[.*\])\)", response.text, re.S)
        if not match:
            raise RuntimeError("unexpected sina response")
        rows = json.loads(match.group(1))
        if rows:
            return rows, "sina:kline", None
        errors.append("sina: empty benchmark response")
    except Exception as exc:  # pragma: no cover - network/source dependent
        errors.append(f"sina: {exc}")

    return [], "", "; ".join(errors)


def parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if hasattr(value, "date"):
        return value.date()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(str(value)[:10].replace("/", "-"), fmt).date()
        except ValueError:
            continue
    return None


def parse_float(value) -> float | None:
    if value in (None, "", "--"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def padded_range(values: list[float]) -> tuple[float, float]:
    if not values:
        return -0.1, 0.1
    low = min(values)
    high = max(values)
    if low == high:
        low -= 0.05
        high += 0.05
    padding = max((high - low) * 0.12, 0.02)
    return low - padding, high + padding


def svg_path(points: list[ChartPoint], y_min: float, y_max: float) -> str:
    if len(points) < 2:
        return ""
    start = points[0].date
    end = points[-1].date
    coords = [point_to_svg(point.date, point.value, start, end, y_min, y_max) for point in points]
    return " ".join(("M" if index == 0 else "L") + f"{x:.2f},{y:.2f}" for index, (x, y) in enumerate(coords))


def svg_area_path(points: list[ChartPoint], y_min: float, y_max: float) -> str:
    if len(points) < 2:
        return ""
    start = points[0].date
    end = points[-1].date
    coords = [point_to_svg(point.date, point.value, start, end, y_min, y_max) for point in points]
    parts = ["M" + f"{coords[0][0]:.2f},{coords[0][1]:.2f}"]
    for x, y in coords:
        parts.append(f"L{x:.2f},{y:.2f}")
    parts.append(f"L{coords[-1][0]:.2f},100 L{coords[0][0]:.2f},100 Z")
    return " ".join(parts)


def marker_positions(
    markers: list[TradeMarker],
    start: date,
    end: date,
    y_min: float,
    y_max: float,
) -> list[dict[str, Any]]:
    result = []
    for marker in markers:
        x, y = point_to_svg(marker.date, marker.value, start, end, y_min, y_max)
        result.append(
            {
                "x": x,
                "y": y,
                "date": marker.date,
                "action": marker.action,
                "amount": marker.amount,
                "share": marker.share,
            }
        )
    return result


def point_to_svg(point_date: date, value: float, start: date, end: date, y_min: float, y_max: float) -> tuple[float, float]:
    width = 100.0
    height = 100.0
    total_days = max((end - start).days, 1)
    x = ((point_date - start).days / total_days) * width
    y = height - ((value - y_min) / (y_max - y_min)) * height
    return max(0.0, min(width, x)), max(0.0, min(height, y))


def y_ticks(y_min: float, y_max: float) -> list[dict[str, Any]]:
    ticks = []
    for ratio in (0.0, 0.5, 1.0):
        value = y_max - (y_max - y_min) * ratio
        ticks.append({"y": ratio * 100, "label": format_return(value)})
    return ticks


def format_return(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def build_aggregate_charts(session: Session) -> list[FundPerformanceChart]:
    """Build aggregate charts: one overall, one per platform."""
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
        navs = session.exec(select(FundNav).where(FundNav.fund_code == code, FundNav.nav_date >= min_date).order_by(FundNav.nav_date)).all()
        if navs:
            all_navs[code] = navs
    
    # 总体聚合
    overall_points = _aggregate_value_over_time(session, active_codes, all_navs)
    by_platform = {}
    for code in active_codes:
        rule = rules.get(code)
        pf = rule.platform if rule and rule.platform else "未分类"
        by_platform.setdefault(pf, set()).add(code)
    
    charts = []
    # 总体
    if overall_points and len(overall_points) >= 2:
        positions = calculate_position_summaries(session)
        active = [p for p in positions if not p.is_closed]
        total_cost = sum(p.cost for p in active)
        total_market = sum(p.market_value for p in active)
        latest_val = overall_points[-1].value
        total_return = (total_market - total_cost) / total_cost if total_cost > 0 else 0
        benchmark_points = benchmark_points_for_range(session, overall_points[0].date, overall_points[-1].date)
        y_min, y_max = padded_range([p.value for p in overall_points + benchmark_points])
        charts.append(FundPerformanceChart(
            fund_code="TOTAL", fund_name="总体持仓",
            latest_return=total_return,
            benchmark_return=benchmark_points[-1].value if benchmark_points else None,
            excess_return=total_return - (benchmark_points[-1].value if benchmark_points else 0),
            fund_points=overall_points, benchmark_points=benchmark_points,
            trade_markers=[], svg_path=svg_path(overall_points, y_min, y_max),
            svg_area=svg_area_path(overall_points, y_min, y_max),
            benchmark_path=svg_path(benchmark_points, y_min, y_max),
            benchmark_area=svg_area_path(benchmark_points, y_min, y_max),
            marker_positions=[], y_ticks=y_ticks(y_min, y_max),
            start_date=overall_points[0].date, end_date=overall_points[-1].date,
        ))
    
    # 分平台
    for pf_name, pf_codes in sorted(by_platform.items()):
        pf_points = _aggregate_value_over_time(session, pf_codes, all_navs)
        if not pf_points or len(pf_points) < 2:
            continue
        pf_cost = sum(p.cost for p in active if p.fund_code in pf_codes)
        pf_ret = (pf_points[-1].value - pf_cost) / pf_cost if pf_cost > 0 else 0
        bm_points = benchmark_points_for_range(session, pf_points[0].date, pf_points[-1].date)
        ymin, ymax = padded_range([p.value for p in pf_points + bm_points])
        charts.append(FundPerformanceChart(
            fund_code=pf_name, fund_name=pf_name,
            latest_return=pf_ret,
            benchmark_return=bm_points[-1].value if bm_points else None,
            excess_return=pf_ret - (bm_points[-1].value if bm_points else 0),
            fund_points=pf_points, benchmark_points=bm_points,
            trade_markers=[], svg_path=svg_path(pf_points, ymin, ymax),
            svg_area=svg_area_path(pf_points, ymin, ymax),
            benchmark_path=svg_path(bm_points, ymin, ymax),
            benchmark_area=svg_area_path(bm_points, ymin, ymax),
            marker_positions=[], y_ticks=y_ticks(ymin, ymax),
            start_date=pf_points[0].date, end_date=pf_points[-1].date,
        ))
    return charts


def _aggregate_value_over_time(
    session: Session,
    fund_codes: set[str],
    all_navs: dict[str, list[FundNav]],
) -> list[ChartPoint]:
    txs = session.exec(select(FundTransaction).where(
        FundTransaction.fund_code.in_(fund_codes)
    ).order_by(FundTransaction.trade_date, FundTransaction.id)).all()
    date_values = {}
    for code in fund_codes:
        navs = all_navs.get(code, [])
        shares_over_time = _holding_shares_over_time(
            [t for t in txs if t.fund_code == code], [n.nav_date for n in navs]
        )
        for n in navs:
            shares = shares_over_time.get(n.nav_date, 0.0)
            val = shares * n.unit_nav
            date_values[n.nav_date] = date_values.get(n.nav_date, 0.0) + val
    if not date_values:
        return []
    sorted_dates = sorted(date_values.keys())
    points = [ChartPoint(d, date_values[d]) for d in sorted_dates if date_values[d] > 10]
    if len(points) < 2:
        return []
    return points


def _holding_shares_over_time(
    fund_txs: list[FundTransaction],
    nav_dates: list[date],
) -> dict[date, float]:
    nav_set = set(nav_dates)
    txs_sorted = sorted(fund_txs, key=lambda t: (t.trade_date, 0 if t.action in {TransactionAction.buy, TransactionAction.dividend_reinvest} else 1, t.id or 0))
    result = {}
    shares = 0.0
    tx_idx = 0
    for d in sorted(nav_set):
        while tx_idx < len(txs_sorted) and txs_sorted[tx_idx].trade_date <= d:
            tx = txs_sorted[tx_idx]
            if tx.action == TransactionAction.buy:
                shares += tx.share or 0.0
            elif tx.action == TransactionAction.sell:
                shares -= tx.share or 0.0
            elif tx.action == TransactionAction.dividend_reinvest:
                shares += tx.share or 0.0
            tx_idx += 1
        result[d] = shares
    return result

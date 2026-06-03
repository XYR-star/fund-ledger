import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlmodel import Session, select

from .models import BenchmarkNav, FundNav, FundTransaction, TransactionAction
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
    charts = []
    for fund_code in funds:
        fund_txs = [tx for tx in txs if tx.fund_code == fund_code]
        if not fund_txs:
            continue
        fund_name = next((tx.fund_name for tx in reversed(fund_txs) if tx.fund_name), "")
        navs = session.exec(
            select(FundNav).where(FundNav.fund_code == fund_code).order_by(FundNav.nav_date)
        ).all()
        if len(navs) < 2:
            continue
        start_date = min(tx.trade_date for tx in fund_txs)
        fund_points = normalize_nav_points(
            [(item.nav_date, item.unit_nav) for item in navs if item.nav_date >= start_date]
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

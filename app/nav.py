from dataclasses import dataclass
from datetime import date, datetime
from io import StringIO

from sqlmodel import Session, select

from .models import FundNav


@dataclass(frozen=True)
class NavRow:
    nav_date: date
    unit_nav: float
    accumulated_nav: float | None = None
    daily_return: float | None = None
    source: str = ""


class NavProvider:
    name = "base"

    def fetch(self, fund_code: str, pz: int = 40000) -> list[NavRow]:
        raise NotImplementedError


class EFinanceNavProvider(NavProvider):
    name = "efinance"

    def fetch(self, fund_code: str, pz: int = 40000) -> list[NavRow]:
        import efinance as ef

        df = ef.fund.get_quote_history(fund_code, pz=pz)
        if df is None or df.empty:
            return []
        rows = []
        for _, row in df.iterrows():
            nav_date = _parse_date(row.get("净值日期") or row.get("日期"))
            unit_nav = _parse_float(row.get("单位净值"))
            if nav_date is None or unit_nav is None:
                continue
            rows.append(
                NavRow(
                    nav_date=nav_date,
                    unit_nav=unit_nav,
                    accumulated_nav=_parse_float(row.get("累计净值")),
                    daily_return=_parse_percent(row.get("涨跌幅")),
                    source=self.name,
                )
            )
        return rows


class EastMoneyLsjzNavProvider(NavProvider):
    name = "eastmoney_lsjz"

    def fetch(self, fund_code: str, pz: int = 40000) -> list[NavRow]:
        import pandas as pd
        import requests

        url = "https://fund.eastmoney.com/f10/F10DataApi.aspx"
        params = {"type": "lsjz", "code": fund_code, "page": 1, "per": min(max(pz, 1), 50000)}
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        tables = pd.read_html(StringIO(response.text))
        if not tables:
            return []
        df = tables[0]
        rows = []
        for _, row in df.iterrows():
            nav_date = _parse_date(row.get("净值日期"))
            unit_nav = _parse_float(row.get("单位净值"))
            if nav_date is None or unit_nav is None:
                continue
            rows.append(
                NavRow(
                    nav_date=nav_date,
                    unit_nav=unit_nav,
                    accumulated_nav=_parse_float(row.get("累计净值")),
                    daily_return=_parse_percent(row.get("日增长率")),
                    source=self.name,
                )
            )
        return rows


DEFAULT_NAV_PROVIDERS: list[NavProvider] = [EFinanceNavProvider(), EastMoneyLsjzNavProvider()]


def sync_nav_for_fund(session: Session, fund_code: str, pz: int = 40000, providers: list[NavProvider] | None = None) -> tuple[int, str | None]:
    errors = []
    for provider in providers or DEFAULT_NAV_PROVIDERS:
        try:
            rows = provider.fetch(fund_code, pz=pz)
        except Exception as exc:  # pragma: no cover - network/source dependent
            errors.append(f"{provider.name}: {exc}")
            continue
        if not rows:
            errors.append(f"{provider.name}: empty nav response")
            continue
        return upsert_nav_rows(session, fund_code, rows), None
    return 0, "; ".join(errors) if errors else "no nav provider configured"


def upsert_nav_rows(session: Session, fund_code: str, rows: list[NavRow]) -> int:
    dates = [item.nav_date for item in rows]
    existing_by_date = {
        item.nav_date: item
        for item in session.exec(
            select(FundNav).where(
                FundNav.fund_code == fund_code,
                FundNav.nav_date.in_(dates),
            )
        ).all()
    }
    inserted = 0
    now = datetime.utcnow()
    for row in rows:
        existing = existing_by_date.get(row.nav_date)
        if existing:
            existing.unit_nav = row.unit_nav
            existing.accumulated_nav = row.accumulated_nav
            existing.daily_return = row.daily_return
            existing.source = row.source
            existing.updated_at = now
            session.add(existing)
        else:
            session.add(
                FundNav(
                    fund_code=fund_code,
                    nav_date=row.nav_date,
                    unit_nav=row.unit_nav,
                    accumulated_nav=row.accumulated_nav,
                    daily_return=row.daily_return,
                    source=row.source,
                    updated_at=now,
                )
            )
            inserted += 1
    session.commit()
    return inserted


def _parse_date(value):
    if value is None:
        return None
    if hasattr(value, "date"):
        return value.date()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(value)[:10], fmt).date()
        except ValueError:
            continue
    return None


def _parse_float(value):
    if value in (None, "", "--"):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except ValueError:
        return None


def _parse_percent(value):
    parsed = _parse_float(value)
    if parsed is None:
        return None
    return parsed / 100

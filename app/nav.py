from datetime import datetime

from sqlmodel import Session, select

from .models import FundNav


def sync_nav_for_fund(session: Session, fund_code: str, pz: int = 40000) -> tuple[int, str | None]:
    try:
        import efinance as ef

        df = ef.fund.get_quote_history(fund_code, pz=pz)
    except Exception as exc:  # pragma: no cover - network/source dependent
        return 0, str(exc)

    if df is None or df.empty:
        return 0, "empty nav response"

    rows = []
    for _, row in df.iterrows():
        nav_date = _parse_date(row.get("净值日期") or row.get("日期"))
        unit_nav = _parse_float(row.get("单位净值"))
        if nav_date is None or unit_nav is None:
            continue
        rows.append(
            (
                nav_date,
                {
                    "unit_nav": unit_nav,
                    "accumulated_nav": _parse_float(row.get("累计净值")),
                    "daily_return": _parse_percent(row.get("涨跌幅")),
                    "updated_at": datetime.utcnow(),
                },
            )
        )
    if not rows:
        return 0, "empty nav response"

    dates = [item[0] for item in rows]
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
    for nav_date, values in rows:
        existing = existing_by_date.get(nav_date)
        if existing:
            for key, value in values.items():
                setattr(existing, key, value)
        else:
            session.add(FundNav(fund_code=fund_code, nav_date=nav_date, **values))
            inserted += 1
    session.commit()
    return inserted, None


def _parse_date(value):
    if value is None:
        return None
    if hasattr(value, "date"):
        return value.date()
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
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

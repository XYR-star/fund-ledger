import re
from dataclasses import dataclass
from datetime import datetime
from multiprocessing import Process, Queue
from queue import Empty
from typing import Any


@dataclass
class SyncedRule:
    fund_code: str
    fund_name: str = ""
    buy_confirm_days: int | None = None
    sell_confirm_days: int | None = None
    cutoff_time: str = "15:00"
    buy_fee_rate: float | None = None
    fee_tiers: list[tuple[int, int | None, float]] | None = None
    source: str = ""
    raw_notes: str = ""


AKSHARE_TIMEOUT_SECONDS = 25


def fetch_fund_rule_from_akshare(fund_code: str) -> SyncedRule:
    import akshare as ak

    code = fund_code.zfill(6)
    result = SyncedRule(fund_code=code, source="akshare")
    notes: list[str] = []

    try:
        overview = call_with_timeout(ak.fund_overview_em, symbol=code)
        result.fund_name = _first_value(overview, ("基金简称", "基金全称")) or ""
    except Exception as exc:
        notes.append(f"overview failed: {exc}")

    try:
        confirm_df = call_with_timeout(ak.fund_fee_em, symbol=code, indicator="交易确认日")
        buy_days, sell_days = parse_confirm_days(confirm_df)
        result.buy_confirm_days = buy_days
        result.sell_confirm_days = sell_days
    except Exception as exc:
        notes.append(f"confirm days failed: {exc}")

    try:
        buy_df = call_with_timeout(ak.fund_fee_em, symbol=code, indicator="申购费率")
    except Exception:
        try:
            buy_df = call_with_timeout(ak.fund_fee_em, symbol=code, indicator="申购费率（前端）")
        except Exception as exc:
            buy_df = None
            notes.append(f"buy fee failed: {exc}")
    if buy_df is not None:
        result.buy_fee_rate = parse_buy_fee_rate(buy_df)

    try:
        redeem_df = call_with_timeout(ak.fund_fee_em, symbol=code, indicator="赎回费率")
        result.fee_tiers = parse_redemption_fee_tiers(redeem_df)
    except Exception as exc:
        notes.append(f"redemption fee failed: {exc}")

    if not result.fee_tiers or result.buy_confirm_days is None or result.sell_confirm_days is None:
        try:
            detail_df = call_with_timeout(ak.fund_individual_detail_info_xq, symbol=code)
            fallback = parse_xueqiu_detail(detail_df)
            result.buy_fee_rate = result.buy_fee_rate if result.buy_fee_rate is not None else fallback.buy_fee_rate
            result.fee_tiers = result.fee_tiers or fallback.fee_tiers
        except Exception as exc:
            notes.append(f"xueqiu fallback failed: {exc}")

    result.raw_notes = "\n".join(notes)
    return result


def call_with_timeout(func, **kwargs):
    queue = Queue(maxsize=1)
    process = Process(target=_call_worker, args=(func, kwargs, queue))
    process.daemon = True
    process.start()
    try:
        process.join(AKSHARE_TIMEOUT_SECONDS)
        if process.is_alive():
            _stop_process(process)
            raise RuntimeError(f"{func.__name__} timed out after {AKSHARE_TIMEOUT_SECONDS}s")
        try:
            ok, payload = queue.get_nowait()
        except Empty as exc:
            raise RuntimeError(f"{func.__name__} returned no result") from exc
        if ok:
            return payload
        raise RuntimeError(str(payload))
    finally:
        _stop_process(process)
        queue.close()
        queue.join_thread()


def _call_worker(func, kwargs, queue):
    try:
        queue.put((True, func(**kwargs)))
    except Exception as exc:
        queue.put((False, exc))


def _stop_process(process: Process) -> None:
    if not process.is_alive():
        process.join(0)
        return
    process.terminate()
    process.join(2)
    if process.is_alive():
        process.kill()
        process.join(2)


def parse_confirm_days(df: Any) -> tuple[int | None, int | None]:
    buy_days = None
    sell_days = None
    for row in _rows(df):
        text = _row_text(row)
        days = parse_t_plus_days(text)
        if days is None:
            continue
        if any(word in text for word in ("买入", "申购", "认购")):
            buy_days = days
        if any(word in text for word in ("卖出", "赎回")):
            sell_days = days
    return buy_days, sell_days


def parse_t_plus_days(text: str) -> int | None:
    match = re.search(r"T\s*[+＋]\s*(\d+)", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def parse_buy_fee_rate(df: Any) -> float | None:
    rates = []
    for row in _rows(df):
        text = _row_text(row)
        if "每笔" in text:
            continue
        rate = parse_rate(text)
        if rate is not None:
            rates.append(rate)
    return min(rates) if rates else None


def parse_redemption_fee_tiers(df: Any) -> list[tuple[int, int | None, float]]:
    tiers = []
    for row in _rows(df):
        text = _row_text(row)
        rate = parse_rate(text)
        if rate is None:
            continue
        min_days, max_days = parse_holding_days_range(text)
        if max_days is None or max_days > min_days:
            tiers.append((min_days, max_days, rate))
    return sorted(_dedupe_tiers(tiers), key=lambda item: item[0])


def parse_xueqiu_detail(df: Any) -> SyncedRule:
    result = SyncedRule(fund_code="", source="akshare:xueqiu")
    tiers = []
    buy_rates = []
    for row in _rows(df):
        text = _row_text(row)
        rate = parse_rate(text)
        if rate is None:
            continue
        if "卖出规则" in text or "赎回" in text or "持有期限" in text:
            min_days, max_days = parse_holding_days_range(text)
            if max_days is None or max_days > min_days:
                tiers.append((min_days, max_days, rate))
        elif "买入规则" in text or "申购" in text:
            buy_rates.append(rate)
    result.fee_tiers = sorted(_dedupe_tiers(tiers), key=lambda item: item[0])
    result.buy_fee_rate = min(buy_rates) if buy_rates else None
    return result


def parse_rate(text: str) -> float | None:
    normalized = text.replace("％", "%")
    if "0费率" in normalized or "免费" in normalized:
        return 0.0
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*%", normalized)
    if matches:
        return float(matches[-1]) / 100
    numeric = re.findall(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", normalized)
    if not numeric:
        return None
    value = float(numeric[-1])
    return value / 100 if value > 0.1 else value


def parse_holding_days_range(text: str) -> tuple[int, int | None]:
    normalized = (
        text.replace("（", "(")
        .replace("）", ")")
        .replace("〈", "<")
        .replace("＜", "<")
        .replace("≤", "<=")
        .replace("≥", ">=")
        .replace("－", "-")
        .replace("—", "-")
    )
    values = re.findall(r"(\d+(?:\.\d+)?)\s*(天|日|个月|月|年)", normalized)
    day_values = [duration_to_days(float(value), unit) for value, unit in values]
    has_lower_bound = any(word in normalized for word in ("大于等于", "不低于", "不少于")) or ">=" in normalized
    has_upper_bound = any(word in normalized for word in ("小于", "少于", "以下", "以内")) or "<" in normalized
    if has_lower_bound and has_upper_bound and len(day_values) >= 2:
        return day_values[0], day_values[1]
    if any(word in normalized for word in ("以上", "及以上", "不低于", "大于等于")) or ">=" in normalized:
        return (day_values[0] if day_values else 0), None
    if any(word in normalized for word in ("以内", "以下", "小于", "少于", "<")):
        return 0, day_values[0] if day_values else None
    if "-" in normalized and len(day_values) >= 2:
        return day_values[0], day_values[1]
    if len(day_values) >= 2:
        return day_values[0], day_values[1]
    if len(day_values) == 1:
        return 0, day_values[0]
    return 0, None


def duration_to_days(value: float, unit: str) -> int:
    if unit in {"天", "日"}:
        return int(value)
    if unit in {"个月", "月"}:
        return int(value * 30)
    if unit == "年":
        return int(value * 365)
    return int(value)


def _rows(df: Any) -> list[dict[str, Any]]:
    if df is None:
        return []
    if hasattr(df, "to_dict"):
        return list(df.to_dict(orient="records"))
    if isinstance(df, list):
        return [row if isinstance(row, dict) else {"value": row} for row in df]
    return []


def _row_text(row: dict[str, Any]) -> str:
    return " ".join(str(value) for value in row.values() if value not in (None, ""))


def _first_value(df: Any, columns: tuple[str, ...]) -> str | None:
    rows = _rows(df)
    if not rows:
        return None
    row = rows[0]
    for column in columns:
        value = row.get(column)
        if value:
            return str(value)
    return None


def _dedupe_tiers(tiers: list[tuple[int, int | None, float]]) -> list[tuple[int, int | None, float]]:
    seen = set()
    result = []
    for tier in tiers:
        key = (tier[0], tier[1], tier[2])
        if key not in seen:
            seen.add(key)
            result.append(tier)
    return result


def sync_timestamp() -> datetime:
    return datetime.utcnow()

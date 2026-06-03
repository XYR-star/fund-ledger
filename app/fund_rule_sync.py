import ast
import re
from dataclasses import dataclass
from datetime import datetime
from multiprocessing import Process, Queue
from queue import Empty
from typing import Any
from urllib.parse import quote


@dataclass
class SyncedRule:
    fund_code: str
    fund_name: str = ""
    fund_type: str = ""
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
        result.fund_type = _first_value(overview, ("基金类型",)) or ""
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


_fund_list_cache: Any = None
_fund_list_failed: bool = False


def search_fund_by_name(fund_name: str) -> dict[str, str] | None:
    global _fund_list_cache, _fund_list_failed
    if _fund_list_failed:
        return search_fund_by_name_sina(fund_name) or search_fund_by_name_eastmoney(fund_name)
    try:
        if _fund_list_cache is None:
            import akshare as ak
            _fund_list_cache = ak.fund_name_em()
    except Exception:
        _fund_list_failed = True
        return search_fund_by_name_sina(fund_name) or search_fund_by_name_eastmoney(fund_name)
    df = _fund_list_cache
    clean = fund_name.replace("（", "(").replace("）", ")").strip()
    hw = clean.replace("(", "（").replace(")", "）").strip()
    for variant in {fund_name.strip(), clean, hw}:
        if not variant:
            continue
        if variant in df["基金简称"].values:
            row = df[df["基金简称"] == variant].iloc[0]
            return {"fund_code": str(row["基金代码"]).zfill(6), "fund_name": str(row["基金简称"]), "fund_type": str(row["基金类型"])}
    for search in [clean, hw, fund_name.strip()]:
        if not search:
            continue
        matches = df[df["基金简称"].str.contains(search, na=False, regex=False)]
        if len(matches) == 1:
            row = matches.iloc[0]
            return {"fund_code": str(row["基金代码"]).zfill(6), "fund_name": str(row["基金简称"]), "fund_type": str(row["基金类型"])}
        if len(matches) > 1:
            short = matches[matches["基金简称"].str.len() == matches["基金简称"].str.len().min()]
            if len(short) >= 1:
                row = short.iloc[0]
                return {"fund_code": str(row["基金代码"]).zfill(6), "fund_name": str(row["基金简称"]), "fund_type": str(row["基金类型"])}
    for search in [clean, hw]:
        if not search:
            continue
        tokens = re.split(r"[()（）\s\-]+", search)
        if len(tokens) > 1:
            for i in range(len(tokens)):
                shorter = "".join(tokens[:i] + tokens[i + 1:])
                if len(shorter) < 3:
                    continue
                matches = df[df["基金简称"].str.contains(shorter, na=False, regex=False)]
                if len(matches) == 1:
                    row = matches.iloc[0]
                    return {"fund_code": str(row["基金代码"]).zfill(6), "fund_name": str(row["基金简称"]), "fund_type": str(row["基金类型"])}
    for search in [clean, hw]:
        base = re.sub(r"[\s]*[（(][^）)]*[）)]", "", search)
        for noise in ["中国", "指数", "混合", "债券", "发起式", "ETF"]:
            shorter = base.replace(noise, "")
            if len(shorter) < 4:
                continue
            matches = df[df["基金简称"].str.contains(shorter, na=False, regex=False)]
            if len(matches) == 1:
                row = matches.iloc[0]
                return {"fund_code": str(row["基金代码"]).zfill(6), "fund_name": str(row["基金简称"]), "fund_type": str(row["基金类型"])}
            if len(matches) > 1:
                short = matches[matches["基金简称"].str.len() == matches["基金简称"].str.len().min()]
                if len(short) >= 1:
                    row = short.iloc[0]
                    return {"fund_code": str(row["基金代码"]).zfill(6), "fund_name": str(row["基金简称"]), "fund_type": str(row["基金类型"])}
    fallback = _search_rows_by_core(fund_name, _rows(df))
    if fallback:
        return fallback
    sina = search_fund_by_name_sina(fund_name)
    if sina:
        return sina
    eastmoney = search_fund_by_name_eastmoney(fund_name)
    if eastmoney:
        return eastmoney
    return None


def search_fund_by_name_sina(fund_name: str) -> dict[str, str] | None:
    try:
        import requests

        url = f"https://suggest3.sinajs.cn/suggest/type=&key={quote(fund_name)}"
        response = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        response.encoding = "gbk"
        match = re.search(r'var\s+suggestvalue\s*=\s*"(.*)"\s*;', response.text, re.S)
        if not match:
            return None
        rows = []
        seen = set()
        for item in match.group(1).split(";"):
            parts = item.split(",")
            if len(parts) < 7:
                continue
            code = parts[2].strip()
            name = (parts[4] or parts[6]).strip()
            if not re.fullmatch(r"\d{6}", code) or not name:
                continue
            key = (code, name)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"基金代码": code, "基金简称": name, "基金类型": "场外基金"})
        return _search_rows_by_core(fund_name, rows)
    except Exception:
        return None


def search_fund_by_name_eastmoney(fund_name: str) -> dict[str, str] | None:
    try:
        import requests

        response = requests.get("https://fund.eastmoney.com/js/fundcode_search.js", timeout=30)
        response.raise_for_status()
        match = re.search(r"var\s+r\s*=\s*(\[.*\]);?", response.text, re.S)
        if not match:
            return None
        rows = []
        for item in ast.literal_eval(match.group(1)):
            if len(item) < 4:
                continue
            rows.append({"基金代码": item[0], "基金简称": item[2], "基金类型": item[3]})
        return _search_rows_by_core(fund_name, rows)
    except Exception:
        return None


def _search_rows_by_core(fund_name: str, rows: list[dict[str, Any]]) -> dict[str, str] | None:
    scored = []
    query_manager = _fund_manager(fund_name)
    query_core = _fund_name_core(fund_name)
    query_constraints = _fund_name_constraints(fund_name)
    if len(query_core) < 8:
        return None
    for row in rows:
        row_name = str(row.get("基金简称") or "")
        if query_manager and _fund_manager(row_name) != query_manager:
            continue
        if not _constraints_match(query_constraints, row_name):
            continue
        row_core = _fund_name_core(row_name)
        if len(row_core) < 8:
            continue
        if query_core == row_core:
            return {
                "fund_code": str(row["基金代码"]).zfill(6),
                "fund_name": str(row["基金简称"]),
                "fund_type": str(row["基金类型"]),
            }
        score = _substring_score(query_core, row_core)
        if score >= 0.78:
            scored.append((score, len(row_core), row))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    best = scored[0]
    if len(scored) > 1 and best[0] - scored[1][0] < 0.08:
        return None
    row = best[2]
    return {
        "fund_code": str(row["基金代码"]).zfill(6),
        "fund_name": str(row["基金简称"]),
        "fund_type": str(row["基金类型"]),
    }


def _fund_manager(value: str) -> str | None:
    text = value.replace("（", "(").replace("）", ")").upper()
    managers = [
        "易方达",
        "华泰柏瑞",
        "鹏华",
        "嘉实",
        "华宝",
        "鑫元",
        "招商",
        "富国",
        "广发",
        "南方",
        "华夏",
        "汇添富",
        "博时",
        "国泰",
        "天弘",
    ]
    for manager in managers:
        if text.startswith(manager.upper()):
            return manager
    return None


def _fund_name_core(value: str) -> str:
    text = value.replace("（", "(").replace("）", ")").upper()
    text = re.sub(r"[\s()（）\-]+", "", text)
    manager = _fund_manager(value)
    if manager:
        text = text[len(manager):]
    replacements = {
        "高股息": "红利",
        "低波动": "低波",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    for noise in ("基金", "指数型", "混合型", "发起式"):
        text = text.replace(noise, "")
    return text


def _fund_name_constraints(value: str) -> dict[str, bool]:
    text = value.replace("（", "(").replace("）", ")").upper()
    return {
        "港股通": "港股通" in text,
    }


def _constraints_match(constraints: dict[str, bool], row_name: str) -> bool:
    row_text = row_name.replace("（", "(").replace("）", ")").upper()
    for keyword, required in constraints.items():
        if required and keyword.upper() not in row_text:
            return False
    return True


def _substring_score(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if left in right or right in left:
        return min(len(left), len(right)) / max(len(left), len(right))
    previous = [0] * (len(right) + 1)
    for left_char in left:
        current = [0]
        for index, right_char in enumerate(right, start=1):
            if left_char == right_char:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[-1]))
        previous = current
    return previous[-1] / max(len(left), len(right))

import json
import re
import threading
import time as time_module
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlmodel import Session, desc, select
from starlette.status import HTTP_303_SEE_OTHER

from .app_settings import configured, masked, runtime_settings, save_settings
from .auth import add_session_middleware, current_user, login_user, logout_user, verify_login
from .config import ensure_data_dirs, settings
from .db import engine, get_session, init_db
from .extractors import extract_candidates, hash_content, hash_file
from .fund_rule_sync import _fund_name_core, fetch_fund_rule_from_akshare, search_fund_by_name, sync_timestamp
from .jobs import create_and_enqueue, recover_interrupted_jobs, register_job
from .llm import is_deepseek_configured, parse_with_deepseek, resolve_fund_code_by_name
from .models import (
    BackgroundJob,
    BenchmarkNav,
    CandidateStatus,
    AppSetting,
    FundFeeTier,
    FundAlias,
    FundNav,
    FundRule,
    FundTransaction,
    FundTransactionCandidate,
    ImportDocument,
    ImportStatus,
    JobStatus,
    OperationAudit,
    TransactionAction,
)
from .nav import sync_nav_for_fund
from .ocr import recognize_file
from .performance import build_performance_charts, format_return, sync_hs300
from .portfolio import calculate_holdings, calculate_position_summaries, money_fund_codes, xalpha_rows
from .templates import templates


app = FastAPI(title="Fund Ledger")
add_session_middleware(app)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
_scheduler_started = False


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    register_background_jobs()
    normalize_money_fund_records()
    recover_interrupted_jobs()
    start_daily_market_sync_scheduler()


def register_background_jobs() -> None:
    register_job("auto_import", process_auto_import_job)
    register_job("ocr_import", process_ocr_job)
    register_job("parse_import", process_parse_job)
    register_job("sync_nav", process_nav_job)
    register_job("sync_benchmark", process_benchmark_job)
    register_job("sync_fund_rule", process_fund_rule_sync_job)
    register_job("daily_market_sync", process_daily_market_sync_job)
    register_job("auto_backup", process_auto_backup_job)


def start_daily_market_sync_scheduler() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    thread = threading.Thread(
        target=daily_market_sync_scheduler_loop,
        name="fund-ledger-daily-market-sync",
        daemon=True,
    )
    thread.start()


def daily_market_sync_scheduler_loop() -> None:
    while True:
        try:
            maybe_enqueue_daily_market_sync()
            maybe_enqueue_auto_backup()
        except Exception:
            pass
        time_module.sleep(60)


def maybe_enqueue_daily_market_sync(now: datetime | None = None) -> BackgroundJob | None:
    with Session(engine) as session:
        config = runtime_settings(session)
        if config.get("AUTO_MARKET_SYNC_ENABLED", "true") != "true":
            return None
        tz_name = config.get("AUTO_MARKET_SYNC_TIMEZONE", "Asia/Shanghai") or "Asia/Shanghai"
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("Asia/Shanghai")
            tz_name = "Asia/Shanghai"
        now = now.astimezone(tz) if now else datetime.now(tz)
        target_time = parse_optional_time_value(config.get("AUTO_MARKET_SYNC_TIME")) or time(21, 30)
        if now.hour != target_time.hour or now.minute != target_time.minute:
            return None
        today_key = now.date().isoformat()
        if config.get("AUTO_MARKET_SYNC_LAST_RUN_DATE") == today_key:
            return None
        job = create_and_enqueue(
            session,
            "daily_market_sync",
            {"date": today_key, "timezone": tz_name, "scheduled_time": target_time.strftime("%H:%M")},
        )
        save_settings(session, {"AUTO_MARKET_SYNC_LAST_RUN_DATE": today_key})
        return job


def maybe_enqueue_auto_backup(now: datetime | None = None) -> BackgroundJob | None:
    with Session(engine) as session:
        config = runtime_settings(session)
        if config.get("AUTO_BACKUP_ENABLED", "true") != "true":
            return None
        tz_name = config.get("AUTO_BACKUP_TIMEZONE", "Asia/Shanghai") or "Asia/Shanghai"
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("Asia/Shanghai")
            tz_name = "Asia/Shanghai"
        now = now.astimezone(tz) if now else datetime.now(tz)
        target_time = parse_optional_time_value(config.get("AUTO_BACKUP_TIME")) or time(2, 10)
        if now.hour != target_time.hour or now.minute != target_time.minute:
            return None
        today_key = now.date().isoformat()
        if config.get("AUTO_BACKUP_LAST_RUN_DATE") == today_key:
            return None
        job = create_and_enqueue(
            session,
            "auto_backup",
            {"date": today_key, "timezone": tz_name, "scheduled_time": target_time.strftime("%H:%M")},
        )
        save_settings(session, {"AUTO_BACKUP_LAST_RUN_DATE": today_key})
        return job


def audit_log(session: Session, action: str, target_type: str, target_id: str = "", detail: str = "") -> None:
    session.add(
        OperationAudit(
            action=action,
            target_type=target_type,
            target_id=str(target_id or ""),
            detail=detail,
            created_at=datetime.utcnow(),
        )
    )


def require_user(request: Request) -> str:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    return user


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=HTTP_303_SEE_OTHER)


def candidates_url(source_hash: str = "", status: str = "", unmatched: bool = False, quality: str = "") -> str:
    params = {}
    if source_hash:
        params["source_hash"] = source_hash
    if status:
        params["status"] = status
    if unmatched:
        params["unmatched"] = "1"
    if quality:
        params["quality"] = quality
    query = urlencode(params)
    return f"/candidates?{query}" if query else "/candidates"


def safe_candidates_return(return_to: str = "") -> str:
    return return_to if return_to == "/candidates" or return_to.startswith("/candidates?") else "/candidates"


def candidates_redirect_with_message(return_to: str, message: str) -> RedirectResponse:
    target = safe_candidates_return(return_to)
    joiner = "&" if "?" in target else "?"
    return redirect(f"{target}{joiner}{urlencode({'message': message})}")


def transactions_url(
    fund_code: str = "",
    action: str = "",
    date_from: date | None = None,
    date_to: date | None = None,
) -> str:
    params = {}
    if fund_code:
        params["fund_code"] = fund_code
    if action:
        params["action"] = action
    if date_from:
        params["date_from"] = date_from.isoformat()
    if date_to:
        params["date_to"] = date_to.isoformat()
    query = urlencode(params)
    return f"/transactions?{query}" if query else "/transactions"


def safe_transactions_return(return_to: str = "") -> str:
    return return_to if return_to == "/transactions" or return_to.startswith("/transactions?") else "/transactions"


def transactions_redirect_with_message(return_to: str, message: str) -> RedirectResponse:
    target = safe_transactions_return(return_to)
    joiner = "&" if "?" in target else "?"
    return redirect(f"{target}{joiner}{urlencode({'message': message})}")


def serialize_model(model):
    data = model.model_dump()
    for key, value in data.items():
        if isinstance(value, (date, datetime)):
            data[key] = value.isoformat()
        elif isinstance(value, time):
            data[key] = value.strftime("%H:%M")
        elif hasattr(value, "value"):
            data[key] = value.value
    return data


def create_candidates_from_text(
    session: Session,
    raw_text: str,
    source_file: str | None = None,
    source_hash: str | None = None,
    config: dict[str, str] | None = None,
) -> tuple[int, list[str]]:
    table_rows = extract_table_rows_from_ocr(raw_text)
    if table_rows:
        created, warnings = create_candidates_from_rows(
            session,
            table_rows,
            source_file=source_file,
            source_hash=source_hash,
            source_text=raw_text,
        )
        if created:
            return created, warnings
    extracted = extract_candidates(raw_text)
    if not extracted:
        return create_inferred_candidates_from_minimal_text(
            session,
            raw_text,
            source_file=source_file,
            source_hash=source_hash,
            config=config,
        )
    created = 0
    warnings: list[str] = []
    llm_cache: dict[str, str] = {}
    known_names = known_fund_names(session)
    for item in extracted:
        fund_code = item.fund_code.zfill(6)
        item.fund_name = normalize_fund_name_with_aliases(session, item.fund_name)
        item.raw_text = apply_default_fund_aliases(item.raw_text)
        if _is_etf_text(item.raw_text) or _is_etf_text(item.fund_name):
            warnings.append(f"跳过 ETF 基金 {item.fund_name}")
            continue
        if fund_code == "000000" and item.fund_name:
            resolved = _resolve_fund_code(session, item.fund_name, known_names, llm_cache, warnings)
            if resolved:
                fund_code = resolved
        if is_etf_fund(session, fund_code):
            warnings.append(f"跳过 ETF 基金 {fund_code} {item.fund_name}")
            continue
        amount_cny, share, fee, confirm_date, effective_trade_date = apply_trade_calculation(
            session,
            fund_code,
            item.action,
            item.amount_cny,
            item.share,
            item.nav,
            item.trade_date,
            submitted_at=None,
        )
        session.add(
            FundTransactionCandidate(
                status=infer_candidate_status(item.raw_text),
                fund_code=fund_code,
                fund_name=item.fund_name,
                trade_date=effective_trade_date,
                submitted_at=None,
                confirm_date=confirm_date,
                action=item.action,
                amount_cny=amount_cny,
                share=share,
                nav=item.nav,
                fee=fee,
                source_file=source_file,
                source_hash=source_hash,
                raw_text=item.raw_text,
                confidence=item.confidence,
            )
        )
        created += 1
    return created, warnings


def _resolve_fund_code(
    session: Session,
    fund_name: str,
    known_names: dict[str, str],
    llm_cache: dict[str, str],
    warnings: list[str],
) -> str | None:
    if not fund_name:
        return None
    original_name = fund_name
    fund_name = normalize_fund_name_with_aliases(session, fund_name)
    if fund_name != original_name:
        warnings.append(f"名称纠错 {original_name} → {fund_name}")
    if fund_name in llm_cache:
        return llm_cache[fund_name]
    known_code = find_known_fund_code(fund_name, known_names)
    if known_code:
        llm_cache[fund_name] = known_code
        warnings.append(f"本地名称匹配 {fund_name} → {known_code}")
        return known_code
    result = search_fund_by_name(fund_name)
    if not result:
        return None
    code = result["fund_code"]
    llm_cache[fund_name] = code
    known_names[fund_name] = code
    existing = session.get(FundRule, code)
    if not existing:
        rule = FundRule(
            fund_code=code,
            fund_name=result["fund_name"],
            fund_type=result["fund_type"],
            sync_source="resolved",
            synced_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        session.add(rule)
    elif not existing.fund_type:
        existing.fund_type = result["fund_type"]
        session.add(existing)
    # commit minimal rule first so apply_trade_calculation can use the code
    session.commit()
    warnings.append(f"名称匹配 {fund_name} → {code}")
    return code


def find_known_fund_code(fund_name: str, known_names: dict[str, str]) -> str | None:
    if fund_name in known_names:
        return known_names[fund_name]
    query_core = _fund_name_core(fund_name)
    if len(query_core) < 8:
        return None
    matches = [
        code
        for name, code in known_names.items()
        if code != "000000" and _fund_name_core(name) == query_core
    ]
    return matches[0] if len(set(matches)) == 1 else None


DEFAULT_FUND_ALIASES = {
    "5OETF": "50ETF",
    "5OET": "50ET",
    "QDI）": "QDII）",
    "QDI)": "QDII)",
    "（QDI）": "（QDII）",
    "(QDI)": "(QDII)",
    "A(人民": "A(人民币份额)",
    "A（人民": "A（人民币份额）",
}


def fund_alias_pairs(session: Session) -> list[tuple[str, str]]:
    pairs = list(DEFAULT_FUND_ALIASES.items())
    for alias in session.exec(select(FundAlias).order_by(FundAlias.id)).all():
        if alias.pattern:
            pairs.append((alias.pattern, alias.replacement))
    return pairs


def normalize_fund_name_with_aliases(session: Session, value: str) -> str:
    text = value or ""
    for pattern, replacement in fund_alias_pairs(session):
        text = text.replace(pattern, replacement)
    return text


def create_inferred_candidates_from_minimal_text(
    session: Session,
    raw_text: str,
    source_file: str | None = None,
    source_hash: str | None = None,
    config: dict[str, str] | None = None,
) -> tuple[int, list[str]]:
    created = 0
    warnings: list[str] = []
    known_names = known_fund_names(session)
    llm_code_cache: dict[str, str] = {}
    for line in raw_text.splitlines():
        text = normalize_fund_name_with_aliases(session, line.strip())
        if not text or text.startswith("#"):
            continue
        trade_day, submitted_at = extract_trade_datetime(text)
        amount = extract_amount(text)
        if not trade_day:
            warnings.append(f"跳过（无法识别日期）：{text[:60]}")
            continue
        if amount is None or amount <= 0:
            warnings.append(f"跳过（无法识别金额）：{text[:60]}")
            continue
        action = infer_action(text)
        status = infer_candidate_status(text)
        fund_code = extract_fund_code(text)
        fund_name = extract_fund_name(text, fund_code, known_names)
        if fund_code is None and _is_fund_name_garbage(fund_name):
            continue
        if _is_etf_text(text) or _is_etf_text(fund_name):
            warnings.append(f"跳过 ETF 基金 {fund_name or text[:40]}")
            continue
        if not fund_code and fund_name in known_names:
            fund_code = known_names[fund_name]
        if not fund_code or fund_code == "000000":
            resolved = _resolve_fund_code(session, fund_name, known_names, llm_code_cache, warnings)
            if resolved:
                fund_code = resolved
        fund_code = (fund_code or "000000").zfill(6)
        if is_etf_fund(session, fund_code):
            rule = get_fund_rule(session, fund_code)
            warnings.append(f"跳过 ETF 基金 {fund_code} {rule.fund_name or fund_name}")
            continue
        money = is_money_fund(session, fund_code)
        amount_cny, share, fee, confirm_date, effective_trade_date = apply_trade_calculation(
            session,
            fund_code,
            action,
            amount if action != TransactionAction.sell else None,
            amount if action == TransactionAction.sell else None,
            1.0 if money else None,
            trade_day,
            submitted_at=submitted_at,
        )
        nav_value = 1.0 if money else None
        if not money:
            nav_item = session.exec(
                select(FundNav)
                .where(FundNav.fund_code == fund_code, FundNav.nav_date == effective_trade_date)
            ).first()
            nav_value = nav_item.unit_nav if nav_item else None
        session.add(
            FundTransactionCandidate(
                status=status,
                fund_code=fund_code,
                fund_name=fund_name,
                trade_date=effective_trade_date,
                submitted_at=submitted_at,
                confirm_date=confirm_date,
                action=action,
                amount_cny=amount_cny,
                share=share,
                nav=nav_value,
                fee=fee,
                source_file=source_file,
                source_hash=source_hash,
                raw_text=text,
                confidence=0.55 if fund_code == "000000" else 0.75,
            )
        )
        created += 1
    return created, warnings


def known_fund_names(session: Session) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in session.exec(select(FundRule)).all():
        if item.fund_name and item.fund_code:
            names[item.fund_name] = item.fund_code
    for item in session.exec(select(FundTransactionCandidate)).all():
        if item.fund_name and item.fund_code != "000000":
            names[item.fund_name] = item.fund_code
    for item in session.exec(select(FundTransaction)).all():
        if item.fund_name and item.fund_code != "000000":
            names[item.fund_name] = item.fund_code
    for name, code in list(names.items()):
        normalized = normalize_fund_name_with_aliases(session, name)
        if normalized and normalized != name:
            names[normalized] = code
    return names


def is_etf_fund(session: Session, fund_code: str) -> bool:
    if fund_code == "000000":
        return False
    rule = get_fund_rule(session, fund_code)
    if not rule:
        return False
    if rule.fund_type and ("ETF" in rule.fund_type or "场内" in rule.fund_type):
        if "联接" not in rule.fund_type and "连接" not in rule.fund_type:
            return True
    name = rule.fund_name
    if name and "ETF" in name.upper() and "联接" not in name and "连接" not in name:
        return True
    return False


def _is_etf_text(text: str) -> bool:
    upper = text.upper()
    return "ETF" in upper and "联接" not in text and "连接" not in text


def _is_fund_name_garbage(name: str) -> bool:
    if not name or all(ch in "0123456789 :.-/" for ch in name):
        return True
    if len(name) <= 3 and not any("\u4e00" <= ch <= "\u9fff" for ch in name):
        return True
    return False


def is_money_fund(session: Session, fund_code: str) -> bool:
    if fund_code == "000000":
        return False
    rule = get_fund_rule(session, fund_code)
    if not rule or not rule.fund_type:
        return False
    return "货币" in rule.fund_type


def normalize_money_fund_records() -> int:
    with Session(engine) as session:
        money_codes = {
            rule.fund_code
            for rule in session.exec(select(FundRule)).all()
            if "货币" in (rule.fund_type or "")
        }
        changed = 0
        for code in money_codes:
            for item in session.exec(
                select(FundTransactionCandidate).where(FundTransactionCandidate.fund_code == code)
            ).all():
                item_changed = normalize_money_record(item)
                changed += item_changed
                if item_changed:
                    item.updated_at = datetime.utcnow()
                    session.add(item)
            for item in session.exec(
                select(FundTransaction).where(FundTransaction.fund_code == code)
            ).all():
                item_changed = normalize_money_record(item)
                changed += item_changed
                if item_changed:
                    session.add(item)
        if changed:
            session.commit()
        return changed


def normalize_money_record(item: FundTransactionCandidate | FundTransaction) -> int:
    if item.action == TransactionAction.buy:
        canonical = item.amount_cny if item.amount_cny is not None else item.share
        return _apply_money_values(item, canonical, canonical, nav=1.0, fee=0.0)
    if item.action == TransactionAction.sell:
        canonical = item.share if item.share is not None else item.amount_cny
        return _apply_money_values(item, canonical, canonical, nav=1.0, fee=0.0)
    if item.action == TransactionAction.dividend:
        return _apply_money_values(item, item.amount_cny, None, nav=1.0, fee=0.0)
    if item.action == TransactionAction.dividend_reinvest:
        canonical = item.amount_cny if item.amount_cny is not None else item.share
        return _apply_money_values(item, canonical, canonical, nav=1.0, fee=0.0)
    return _apply_money_values(item, item.amount_cny, item.share, nav=1.0, fee=0.0)


def _apply_money_values(
    item: FundTransactionCandidate | FundTransaction,
    amount: float | None,
    share: float | None,
    nav: float,
    fee: float,
) -> int:
    changed = 0
    amount = round(amount, 2) if amount is not None else None
    share = round(share, 2) if share is not None else None
    for key, value in (("amount_cny", amount), ("share", share), ("nav", nav), ("fee", fee)):
        if getattr(item, key) != value:
            setattr(item, key, value)
            changed = 1
    return changed


def extract_trade_datetime(text: str) -> tuple[date | None, time | None]:
    match = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", text)
    if not match:
        return None, None
    trade_day = parse_date_value(match.group(1))
    time_match = re.search(r"(\d{1,2}):(\d{2})", text)
    if not time_match:
        return trade_day, None
    return trade_day, time(int(time_match.group(1)), int(time_match.group(2)))


def extract_fund_code(text: str) -> str | None:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    return match.group(1) if match else None


def extract_fund_name(text: str, fund_code: str | None, known_names: dict[str, str]) -> str:
    text = apply_default_fund_aliases(text)
    for name in sorted(known_names, key=len, reverse=True):
        if name and name in text:
            return name
    cleaned = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", " ", text)
    cleaned = re.sub(r"\d{1,2}:\d{2}", " ", cleaned)
    if fund_code:
        cleaned = cleaned.replace(fund_code, " ")
    cleaned = re.sub(r"[（(]\s*(?:人民币|美元|港元|后端|前端|现汇|现钞)\s*(?:份额)?[）)]", " ", cleaned)
    noise_words = r"买入|申购|卖出|赎回|成功|失败|撤销|取消|已确认|交易金额|现金分红"
    cleaned = re.sub(noise_words, " ", cleaned)
    cleaned = re.sub(r"(?:^|\s)\d+(?:\.\d+)?\s*(?:元|份)?(?:\s|$)", " ", cleaned)
    cleaned = re.sub(r"[,，.。：:\-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    parts = [part for part in cleaned.split(" ") if part and len(part) >= 2]
    for part in parts:
        if any("\u4e00" <= ch <= "\u9fff" for ch in part):
            return part
    return parts[0] if parts else ""


def apply_default_fund_aliases(value: str) -> str:
    text = value or ""
    for pattern, replacement in DEFAULT_FUND_ALIASES.items():
        text = text.replace(pattern, replacement)
    return text


def normalize_fund_name_with_aliases_no_db(value: str) -> str:
    return apply_default_fund_aliases(value)


def extract_amount(text: str) -> float | None:
    cleaned = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", " ", text)
    cleaned = re.sub(r"\d{1,2}:\d{2}", " ", cleaned)
    cleaned = re.sub(r"(?<!\d)\d{6}(?!\d)", " ", cleaned)
    values = re.findall(r"(?<!\d)(\d+(?:,\d{3})*(?:\.\d+)?|\d+\.\d+)(?!\d)", cleaned)
    if not values:
        return None
    return parse_float_value(values[-1])


def infer_action(text: str) -> TransactionAction:
    if any(word in text for word in ("赎回", "卖出", "sell", "SELL")):
        return TransactionAction.sell
    if "红利再投" in text:
        return TransactionAction.dividend_reinvest
    if "分红" in text:
        return TransactionAction.dividend
    return TransactionAction.buy


def infer_candidate_status(text: str) -> CandidateStatus:
    if any(
        word in text
        for word in (
            "撤销",
            "已撤销",
            "撤单",
            "已撤单",
            "取消",
            "已取消",
            "失败",
            "未成功",
            "不成功",
            "作废",
            "已作废",
            "交易关闭",
            "已关闭",
            "申请失败",
            "确认失败",
            "ignored",
        )
    ):
        return CandidateStatus.ignored
    return CandidateStatus.pending


def infer_candidate_status_for_row(row: dict[str, Any], source_text: str | None = None) -> CandidateStatus:
    source_status = source_status_for_row(row, source_text or "")
    return infer_candidate_status(f"{row} {source_status}")


def extract_table_rows_from_ocr(source_text: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in source_text.splitlines() if line.strip()]
    rows: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = normalize_fund_name_with_aliases_no_db(lines[index])
        if not _looks_like_fund_name_cell(line):
            index += 1
            continue
        chunk = [line]
        for item in lines[index + 1 : index + 18]:
            chunk.append(normalize_fund_name_with_aliases_no_db(item))
            if item == "明细":
                break
        row = table_chunk_to_row(chunk)
        if row:
            rows.append(row)
            index += max(len(chunk), 1)
        else:
            index += 1
    return rows


def _looks_like_fund_name_cell(value: str) -> bool:
    text = value.strip()
    if len(text) < 4 or not any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return False
    if any(word in text for word in ("名称", "创建时间", "交易类型", "交易渠道", "交易账户", "状态", "操作", "金额", "份额")):
        return False
    return any(word in text for word in ("基金", "债", "混合", "指数", "联接", "QDII", "股票", "货币", "短债", "红利", "高股息", "ETF"))


def table_chunk_to_row(chunk: list[str]) -> dict[str, Any] | None:
    joined = " ".join(chunk)
    trade_day, submitted_at = extract_trade_datetime(joined)
    amount = extract_table_trade_value(chunk)
    if not trade_day or amount is None:
        return None
    fund_name = chunk[0].strip()
    fund_code = extract_fund_code(joined) or "000000"
    return {
        "fund_code": fund_code,
        "fund_name": fund_name,
        "trade_date": trade_day.isoformat(),
        "submitted_at": submitted_at.strftime("%H:%M:%S") if submitted_at else "",
        "action": infer_action(joined).value,
        "amount_cny": amount if infer_action(joined) != TransactionAction.sell else None,
        "share": amount if infer_action(joined) == TransactionAction.sell else None,
        "transaction_status": source_status_from_chunk(joined),
        "raw_chunk": joined,
    }


def source_status_from_chunk(text: str) -> str:
    if infer_candidate_status(text) == CandidateStatus.ignored:
        return text
    if any(word in text for word in ("成功", "已确认", "确认成功")):
        return "成功"
    return ""


def extract_table_trade_value(chunk: list[str]) -> float | None:
    action_index = None
    for index, item in enumerate(chunk):
        if any(word in item for word in ("申购", "赎回", "买入", "卖出", "强制调增", "强制调减", "分红")):
            action_index = index
            break
    scan = chunk[action_index + 1 :] if action_index is not None else chunk
    for item in scan:
        if any(word in item for word in ("成功", "失败", "撤销", "取消", "明细", "尾号", "银行", "账户")):
            continue
        value = parse_float_value(item)
        if value is not None:
            return value
    return None


def source_status_for_row(row: dict[str, Any], source_text: str) -> str:
    if not source_text:
        return ""
    fund_name = str(row.get("fund_name") or "").strip()
    trade_date = parse_date_value(row.get("trade_date"))
    if not fund_name or not trade_date:
        return ""
    amount = parse_float_value(row.get("amount_cny") or row.get("amount") or row.get("share"))
    date_text = trade_date.isoformat()
    compact_date = date_text.replace("-", "")
    amount_tokens: set[str] = set()
    if amount is not None:
        amount_tokens.add(f"{amount:.2f}")
        amount_tokens.add(str(int(amount)) if amount.is_integer() else str(amount))
    lines = [line.strip() for line in source_text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if fund_name not in line:
            continue
        chunk = []
        for item in lines[index : index + 14]:
            chunk.append(item)
            if item == "明细":
                break
        joined = " ".join(chunk)
        compact_joined = joined.replace("-", "").replace(" ", "")
        if date_text not in joined and compact_date not in compact_joined:
            continue
        if amount_tokens and not any(token in joined for token in amount_tokens):
            continue
        status = source_status_from_chunk(joined)
        if status:
            return status
    return ""


def find_effective_nav(
    session: Session,
    fund_code: str,
    trade_day: date,
    submitted_at: time | None,
    rule: "FundRule | None" = None,
) -> FundNav | None:
    if fund_code == "000000":
        return None
    cutoff = parse_time_value((rule.cutoff_time if rule else "15:00") or "15:00")
    target = trade_day + timedelta(days=1) if submitted_at and submitted_at >= cutoff else trade_day
    nav_item = session.exec(
        select(FundNav)
        .where(FundNav.fund_code == fund_code, FundNav.nav_date >= target)
        .order_by(FundNav.nav_date)
    ).first()
    if nav_item:
        return nav_item
    sync_nav_for_fund(session, fund_code)
    return session.exec(
        select(FundNav)
        .where(FundNav.fund_code == fund_code, FundNav.nav_date >= target)
        .order_by(FundNav.nav_date)
    ).first()


def find_nth_nav_date(session: Session, fund_code: str, nav_date: date, n: int) -> date | None:
    if n <= 0:
        return nav_date
    items = session.exec(
        select(FundNav)
        .where(FundNav.fund_code == fund_code, FundNav.nav_date > nav_date)
        .order_by(FundNav.nav_date)
    ).all()
    return items[n - 1].nav_date if len(items) >= n else None


def find_next_nav_date(session: Session, fund_code: str, nav_date: date) -> date | None:
    return find_nth_nav_date(session, fund_code, nav_date, 1)


def nav_value_on_date(session: Session, fund_code: str, nav_date: date | None) -> float | None:
    if not nav_date:
        return None
    item = session.exec(
        select(FundNav).where(
            FundNav.fund_code == fund_code,
            FundNav.nav_date == nav_date,
        )
    ).first()
    return item.unit_nav if item else None


def get_fund_rule(session: Session, fund_code: str) -> FundRule:
    existing = session.get(FundRule, fund_code)
    if existing:
        return existing
    return FundRule(fund_code=fund_code)


def parse_time_value(value: str) -> time:
    try:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return time(15, 0)


def parse_optional_time_value(value: Any) -> time | None:
    if value in (None, "", "-", "null"):
        return None
    if isinstance(value, time):
        return value
    match = re.search(r"(\d{1,2}):(\d{2})", str(value).strip())
    if not match:
        return None
    try:
        return time(int(match.group(1)), int(match.group(2)))
    except ValueError:
        return None


def infer_redemption_fee(
    session: Session,
    fund_code: str,
    sold_share: float,
    nav_value: float,
    sell_date: date,
) -> float | None:
    tiers = session.exec(
        select(FundFeeTier)
        .where(FundFeeTier.fund_code == fund_code)
        .order_by(FundFeeTier.min_holding_days)
    ).all()
    if not tiers:
        return None
    remaining_share = sold_share
    total_fee = 0.0
    for lot_share, lot_date in open_lots(session, fund_code):
        if remaining_share <= 0:
            break
        used_share = min(remaining_share, lot_share)
        holding_days = max((sell_date - lot_date).days, 0)
        rate = redemption_rate_for_days(tiers, holding_days)
        total_fee += used_share * nav_value * rate
        remaining_share -= used_share
    return round(total_fee, 2) if sold_share > remaining_share else None


def redemption_rate_for_days(tiers: list[FundFeeTier], holding_days: int) -> float:
    for tier in tiers:
        if holding_days < tier.min_holding_days:
            continue
        if tier.max_holding_days is None or holding_days < tier.max_holding_days:
            return tier.redemption_fee_rate
    return 0.0


def open_lots(session: Session, fund_code: str) -> list[tuple[float, date]]:
    lots: list[tuple[float, date]] = []
    transactions = sorted(
        session.exec(
        select(FundTransaction)
        .where(FundTransaction.fund_code == fund_code)
        .order_by(FundTransaction.trade_date, FundTransaction.id)
        ).all(),
        key=lambda tx: (
            tx.trade_date,
            0 if tx.action in {TransactionAction.buy, TransactionAction.dividend_reinvest} else 1,
            tx.id or 0,
        ),
    )
    for tx in transactions:
        if tx.action in {TransactionAction.buy, TransactionAction.dividend_reinvest} and tx.share:
            lots.append((tx.share, tx.trade_date))
        elif tx.action == TransactionAction.sell and tx.share:
            remaining = tx.share
            new_lots: list[tuple[float, date]] = []
            for lot_share, lot_date in lots:
                if remaining <= 0:
                    new_lots.append((lot_share, lot_date))
                    continue
                used = min(remaining, lot_share)
                remaining -= used
                if lot_share > used:
                    new_lots.append((lot_share - used, lot_date))
            lots = new_lots
    return lots


def apply_trade_calculation(
    session: Session,
    fund_code: str,
    action: TransactionAction,
    amount_cny: float | None,
    share: float | None,
    nav: float | None,
    trade_date: date,
    submitted_at: time | None = None,
) -> tuple[float | None, float | None, float | None, date | None, date]:
    if fund_code == "000000":
        return amount_cny, share, None, None, trade_date
    rule = get_fund_rule(session, fund_code)
    money = is_money_fund(session, fund_code)
    if money:
        confirm_date = find_nth_nav_date(
            session,
            fund_code,
            trade_date,
            rule.buy_confirm_days if action == TransactionAction.buy else rule.sell_confirm_days,
        ) or trade_date
        if action == TransactionAction.buy:
            canonical = amount_cny if amount_cny is not None else share
            return canonical, canonical, 0.0, confirm_date, trade_date
        if action == TransactionAction.sell:
            canonical = share if share is not None else amount_cny
            return canonical, canonical, 0.0, confirm_date, trade_date
        if action == TransactionAction.dividend:
            return amount_cny, None, 0.0, confirm_date, trade_date
        if action == TransactionAction.dividend_reinvest:
            canonical = amount_cny if amount_cny is not None else share
            return canonical, canonical, 0.0, confirm_date, trade_date
        return amount_cny, share, 0.0, confirm_date, trade_date
    effective_nav_item = find_effective_nav(session, fund_code, trade_date, submitted_at, rule)
    effective_trade_date = effective_nav_item.nav_date if effective_nav_item else trade_date
    nav_value = effective_nav_item.unit_nav if effective_nav_item else nav
    if money and nav_value is None:
        nav_value = 1.0
    if nav_value is None:
        return amount_cny, share, None, None, trade_date
    nav_date = effective_trade_date
    confirm_date = find_nth_nav_date(
        session,
        fund_code,
        effective_trade_date,
        rule.buy_confirm_days if action == TransactionAction.buy else rule.sell_confirm_days,
    )
    fee = None
    if action == TransactionAction.buy:
        effective_amount = amount_cny
        if effective_amount and effective_amount > 0:
            buy_fee_rate = 0.0 if money else rule.buy_fee_rate
            fee = round(effective_amount * buy_fee_rate, 2)
            if not share:
                share = round((effective_amount - fee) / nav_value, 2)
        elif share and share > 0:
            amount_cny = round(share * nav_value, 2)
            buy_fee_rate = 0.0 if money else rule.buy_fee_rate
            fee = round(amount_cny * buy_fee_rate, 2)
    elif action == TransactionAction.sell:
        effective_share = share
        if effective_share and effective_share > 0:
            fee = 0.0 if money else infer_redemption_fee(session, fund_code, effective_share, nav_value, nav_date)
            if not amount_cny:
                amount_cny = round(effective_share * nav_value - (fee or 0), 2)
        elif amount_cny and amount_cny > 0:
            share = round(amount_cny / nav_value, 2)
            fee = 0.0 if money else infer_redemption_fee(session, fund_code, share, nav_value, nav_date)
    elif action == TransactionAction.dividend:
        fee = 0.0
        share = None
    elif action == TransactionAction.dividend_reinvest:
        fee = 0.0
        if amount_cny and amount_cny > 0 and not share:
            share = round(amount_cny / nav_value, 2)
        elif share and share > 0 and not amount_cny:
            amount_cny = round(share * nav_value, 2)
    return amount_cny, share, fee, confirm_date, effective_trade_date


def calculate_manual_transaction_values(
    session: Session,
    fund_code: str,
    action: TransactionAction,
    amount_cny: float | None,
    share: float | None,
    nav: float | None,
    fee: float | None,
    trade_date: date,
    submitted_at: time | None,
    confirm_date: date | None,
) -> tuple[float | None, float | None, float | None, date | None, date, float | None]:
    if nav is None:
        amount_cny, share, calc_fee, calc_confirm, calc_trade_date = apply_trade_calculation(
            session,
            fund_code,
            action,
            amount_cny,
            share,
            nav,
            trade_date,
            submitted_at=submitted_at,
        )
        calc_nav = 1.0 if is_money_fund(session, fund_code) else nav_value_on_date(session, fund_code, calc_trade_date)
        return amount_cny, share, fee if fee is not None else calc_fee, confirm_date or calc_confirm, calc_trade_date, calc_nav
    rule = get_fund_rule(session, fund_code)
    money = is_money_fund(session, fund_code)
    nav_value = 1.0 if money else nav
    calc_confirm = confirm_date or find_nth_nav_date(
        session,
        fund_code,
        trade_date,
        rule.buy_confirm_days if action == TransactionAction.buy else rule.sell_confirm_days,
    )
    calc_fee = fee
    if money:
        calc_fee = 0.0 if fee is None else fee
        if action == TransactionAction.buy:
            canonical = amount_cny if amount_cny is not None else share
            return canonical, canonical, calc_fee, calc_confirm or trade_date, trade_date, 1.0
        if action == TransactionAction.sell:
            canonical = share if share is not None else amount_cny
            return canonical, canonical, calc_fee, calc_confirm or trade_date, trade_date, 1.0
    if action == TransactionAction.buy:
        if amount_cny is not None and amount_cny > 0:
            calc_fee = calc_fee if calc_fee is not None else round(amount_cny * rule.buy_fee_rate, 2)
            if share is None:
                share = round((amount_cny - (calc_fee or 0)) / nav_value, 2)
        elif share is not None and share > 0:
            amount_cny = round(share * nav_value, 2)
            calc_fee = calc_fee if calc_fee is not None else round(amount_cny * rule.buy_fee_rate, 2)
    elif action == TransactionAction.sell:
        if share is not None and share > 0:
            calc_fee = calc_fee if calc_fee is not None else infer_redemption_fee(session, fund_code, share, nav_value, trade_date)
            if amount_cny is None:
                amount_cny = round(share * nav_value - (calc_fee or 0), 2)
        elif amount_cny is not None and amount_cny > 0:
            share = round(amount_cny / nav_value, 2)
            calc_fee = calc_fee if calc_fee is not None else infer_redemption_fee(session, fund_code, share, nav_value, trade_date)
    elif action == TransactionAction.dividend:
        share = None
        calc_fee = 0.0 if calc_fee is None else calc_fee
    elif action == TransactionAction.dividend_reinvest:
        calc_fee = 0.0 if calc_fee is None else calc_fee
        if amount_cny is not None and amount_cny > 0 and share is None:
            share = round(amount_cny / nav_value, 2)
        elif share is not None and share > 0 and amount_cny is None:
            amount_cny = round(share * nav_value, 2)
    return amount_cny, share, calc_fee, calc_confirm, trade_date, nav_value


def backfill_incomplete_transaction_values(session: Session) -> int:
    changed = 0
    transactions = session.exec(select(FundTransaction).order_by(FundTransaction.trade_date, FundTransaction.id)).all()
    for tx in transactions:
        if tx.fund_code == "000000":
            continue
        if not _transaction_needs_value_backfill(tx):
            continue
        amount_cny, share, fee, confirm_date, trade_date, nav = calculate_manual_transaction_values(
            session,
            tx.fund_code,
            tx.action,
            tx.amount_cny,
            tx.share,
            tx.nav,
            tx.fee,
            tx.trade_date,
            tx.submitted_at,
            tx.confirm_date,
        )
        item_changed = False
        for key, value in (
            ("amount_cny", amount_cny),
            ("share", share),
            ("fee", fee),
            ("confirm_date", confirm_date),
            ("nav", nav),
        ):
            if getattr(tx, key) is None and value is not None:
                setattr(tx, key, value)
                item_changed = True
        if tx.nav is None and nav is not None and trade_date != tx.trade_date:
            tx.trade_date = trade_date
            item_changed = True
        if item_changed:
            session.add(tx)
            changed += 1
    if changed:
        session.commit()
    return changed


def _transaction_needs_value_backfill(tx: FundTransaction) -> bool:
    if tx.action == TransactionAction.dividend:
        return tx.amount_cny is None or tx.confirm_date is None
    if tx.action in {TransactionAction.buy, TransactionAction.sell, TransactionAction.dividend_reinvest}:
        return tx.share is None or tx.nav is None or tx.confirm_date is None
    return tx.confirm_date is None


def create_candidates_from_rows(
    session: Session,
    rows: list[dict[str, Any]],
    source_file: str | None = None,
    source_hash: str | None = None,
    source_text: str | None = None,
) -> tuple[int, list[str]]:
    created = 0
    warnings: list[str] = []
    llm_cache: dict[str, str] = {}
    known_names = known_fund_names(session)
    for row in rows:
        trade_date = parse_date_value(row.get("trade_date"))
        fund_code = str(row.get("fund_code") or "").strip().zfill(6)
        if not trade_date or not fund_code.isdigit() or len(fund_code) != 6:
            continue
        fund_name = normalize_fund_name_with_aliases(session, str(row.get("fund_name") or ""))
        row["fund_name"] = fund_name
        if _is_etf_text(fund_name) or _is_etf_text(str(row)):
            warnings.append(f"跳过 ETF 基金 {fund_name}")
            continue
        if fund_code == "000000" and fund_name:
            resolved = _resolve_fund_code(session, fund_name, known_names, llm_cache, warnings)
            if resolved:
                fund_code = resolved
        if is_etf_fund(session, fund_code):
            continue
        try:
            action = TransactionAction(str(row.get("action") or TransactionAction.buy.value))
        except ValueError:
            action = TransactionAction.buy
        submitted_at = parse_optional_time_value(
            row.get("submitted_at") or row.get("submitted_time") or row.get("trade_time")
        )
        money = is_money_fund(session, fund_code)
        parsed_amount = parse_float_value(row.get("amount_cny"))
        parsed_share = parse_float_value(row.get("share"))
        parsed_nav = parse_float_value(row.get("nav"))
        parsed_fee = parse_float_value(row.get("fee"))
        parsed_confirm_date = parse_date_value(row.get("confirm_date"))
        if money:
            nav = 1.0
            amount_cny, share, fee, confirm_date, effective_trade_date = apply_trade_calculation(
                session,
                fund_code,
                action,
                parsed_amount,
                parsed_share,
                nav,
                trade_date,
                submitted_at=submitted_at,
            )
        else:
            nav = parsed_nav
            amount_cny = parsed_amount
            share = parsed_share
            fee = parsed_fee
            confirm_date = parsed_confirm_date
            effective_trade_date = trade_date
            if action == TransactionAction.dividend:
                share = None
                fee = 0.0 if fee is None else fee
        source_status = source_status_for_row(row, source_text or "")
        row_raw = {**row, "source_status": source_status} if source_status else row
        candidate = FundTransactionCandidate(
            status=infer_candidate_status(f"{row} {source_status}"),
            fund_code=fund_code,
            fund_name=str(row.get("fund_name") or ""),
            trade_date=effective_trade_date,
            submitted_at=submitted_at,
            confirm_date=confirm_date,
            action=action,
            amount_cny=amount_cny,
            share=share,
            nav=nav,
            fee=fee,
            source_file=source_file,
            source_hash=source_hash,
            raw_text=str(row_raw),
            confidence=0.85,
        )
        session.add(candidate)
        created += 1
    return created, warnings


def parse_date_value(value: Any) -> date | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip().replace("/", "-"))
    except ValueError:
        return None


def parse_float_value(value: Any) -> float | None:
    if value in (None, "", "-", "null"):
        return None
    try:
        return float(str(value).replace(",", "").replace("元", "").replace("份", "").strip())
    except ValueError:
        return None


def parse_int_value(value: Any, default: int | None = None) -> int | None:
    if value in (None, "", "-", "null"):
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        return default


def parse_positive_int_value(value: Any) -> int | None:
    parsed = parse_int_value(value)
    return parsed if parsed and parsed > 0 else None


def process_ocr_job(payload: dict[str, Any]) -> str:
    document_id = int(payload["document_id"])
    with Session(engine) as session:
        document = session.get(ImportDocument, document_id)
        if not document:
            raise RuntimeError("导入文档不存在")
        if not document.source_file:
            raise RuntimeError("没有可 OCR 的文件")
        document.status = ImportStatus.ocr_running
        document.error_message = ""
        document.updated_at = datetime.utcnow()
        session.add(document)
        session.commit()
        try:
            result = recognize_file(document.source_file, runtime_settings(session))
        except Exception as exc:
            document.status = ImportStatus.error
            document.error_message = str(exc)
            document.updated_at = datetime.utcnow()
            session.add(document)
            session.commit()
            raise
        document.ocr_text = result.text
        document.status = ImportStatus.ocr_done
        document.error_message = ""
        document.updated_at = datetime.utcnow()
        session.add(document)
        session.commit()
        return f"OCR 完成，识别 {len(result.text)} 个字符"


import difflib


def _check_content_similarity(
    session: Session, document: ImportDocument
) -> tuple[ImportDocument, float] | None:
    text = document.ocr_text or document.raw_text
    if len(text) < 40:
        return None
    normalized = re.sub(r"\d+", "0", text)
    normalized = re.sub(r"\s+", " ", normalized)
    others = session.exec(
        select(ImportDocument).where(
            ImportDocument.id != document.id,
            ImportDocument.status.notin_([ImportStatus.deleted]),
        ).order_by(desc(ImportDocument.created_at))
    ).all()
    for other in others:
        other_text = other.ocr_text or other.raw_text
        if len(other_text) < 40:
            continue
        other_norm = re.sub(r"\d+", "0", other_text)
        other_norm = re.sub(r"\s+", " ", other_norm)
        ratio = difflib.SequenceMatcher(None, normalized, other_norm).ratio()
        if ratio >= 0.85:
            return other, ratio
    return None


def process_auto_import_job(payload: dict[str, Any]) -> str:
    document_id = int(payload["document_id"])
    messages = []
    parsed_count = 0
    with Session(engine) as session:
        document = session.get(ImportDocument, document_id)
        if not document:
            raise RuntimeError("导入文档不存在")
        config = runtime_settings(session)
        document.error_message = ""
        document.updated_at = datetime.utcnow()
        session.add(document)
        session.commit()

        if document.source_file:
            document.status = ImportStatus.ocr_running
            document.updated_at = datetime.utcnow()
            session.add(document)
            session.commit()
            try:
                result = recognize_file(document.source_file, config)
            except Exception as exc:
                document.status = ImportStatus.error
                document.error_message = f"OCR 失败：{exc}"
                document.updated_at = datetime.utcnow()
                session.add(document)
                session.commit()
                raise
            document.ocr_text = result.text
            document.status = ImportStatus.ocr_done
            document.updated_at = datetime.utcnow()
            session.add(document)
            session.commit()
            messages.append(f"OCR {len(result.text)} 字")

            similar = _check_content_similarity(session, document)
            if similar is not None:
                other_doc, ratio = similar
                document.error_message = (
                    f"⚠ 与导入 #{other_doc.id}「{other_doc.file_name or '手动文本'}」"
                    f"内容高度相似（{ratio:.0%}），可能重复，请核实"
                )
                document.updated_at = datetime.utcnow()
                session.add(document)
                session.commit()
                messages.append(f"发现相似文档 #{other_doc.id}")

        text = document.ocr_text or document.raw_text
        if not text.strip():
            document.status = ImportStatus.error
            document.error_message = "OCR 后没有可解析文本"
            document.updated_at = datetime.utcnow()
            session.add(document)
            session.commit()
            raise RuntimeError(document.error_message)

        if is_deepseek_configured(config):
            try:
                llm_result = parse_with_deepseek(text, config)
            except Exception as exc:
                messages.append(f"DeepSeek 解析失败，已回退规则解析：{exc}")
            else:
                if llm_result and llm_result.parsed_json:
                    document.llm_text = llm_result.raw_response
                    parsed_count, parse_warnings = create_candidates_from_rows(
                        session,
                        llm_result.parsed_json,
                        source_file=document.source_file,
                        source_hash=document.source_hash,
                        source_text=text,
                    )
                    messages.append("DeepSeek 解析")
                    if parse_warnings:
                        messages.extend(parse_warnings)

        if parsed_count == 0:
            parsed_count, parse_warnings = create_candidates_from_text(
                session,
                text,
                source_file=document.source_file,
                source_hash=document.source_hash,
                config=config,
            )
            messages.append("规则解析")
            if parse_warnings:
                messages.extend(parse_warnings)

        document.status = ImportStatus.parse_done
        document.updated_at = datetime.utcnow()
        session.add(document)
        session.commit()

        fund_codes = fund_codes_for_source(session, document.source_hash)
        messages.append(f"候选 {parsed_count} 条")
        if fund_codes:
            messages.append(f"基金 {', '.join(sorted(fund_codes))}")
        _auto_sync_resolved_rules(session)
        return "；".join(messages)


def _auto_sync_resolved_rules(session: Session) -> None:
    resolved = session.exec(
        select(FundRule).where(FundRule.sync_source == "resolved")
    ).all()
    for rule in resolved:
        try:
            synced = fetch_fund_rule_from_akshare(rule.fund_code)
            rule.fund_name = synced.fund_name or rule.fund_name
            rule.fund_type = synced.fund_type or rule.fund_type
            if synced.buy_confirm_days is not None:
                rule.buy_confirm_days = synced.buy_confirm_days
            if synced.sell_confirm_days is not None:
                rule.sell_confirm_days = synced.sell_confirm_days
            if synced.buy_fee_rate is not None:
                rule.buy_fee_rate = synced.buy_fee_rate
            rule.sync_source = synced.source
            rule.synced_at = sync_timestamp()
            rule.updated_at = datetime.utcnow()
            session.add(rule)
            if synced.fee_tiers:
                for t in session.exec(select(FundFeeTier).where(FundFeeTier.fund_code == rule.fund_code)).all():
                    session.delete(t)
                for min_d, max_d, rate in synced.fee_tiers:
                    session.add(FundFeeTier(fund_code=rule.fund_code, min_holding_days=min_d, max_holding_days=max_d, redemption_fee_rate=rate, updated_at=datetime.utcnow()))
        except Exception:
            pass
    if resolved:
        session.commit()


def process_parse_job(payload: dict[str, Any]) -> str:
    document_id = int(payload["document_id"])
    use_llm = bool(payload.get("use_llm"))
    with Session(engine) as session:
        document = session.get(ImportDocument, document_id)
        if not document:
            raise RuntimeError("导入文档不存在")
        text = document.ocr_text or document.raw_text
        if not text.strip():
            raise RuntimeError("没有可解析文本")

        parsed_count = 0
        parse_warnings: list[str] = []
        config = runtime_settings(session)
        if not use_llm:
            document.llm_text = ""
        if use_llm and is_deepseek_configured(config):
            llm_result = parse_with_deepseek(text, config)
            if llm_result and llm_result.parsed_json:
                document.llm_text = llm_result.raw_response
                parsed_count, parse_warnings = create_candidates_from_rows(
                    session,
                    llm_result.parsed_json,
                    source_file=document.source_file,
                    source_hash=document.source_hash,
                    source_text=text,
                )
        if parsed_count == 0:
            parsed_count, parse_warnings2 = create_candidates_from_text(
                session,
                text,
                source_file=document.source_file,
                source_hash=document.source_hash,
                config=config,
            )
            parse_warnings.extend(parse_warnings2)
        document.status = ImportStatus.parse_done
        document.error_message = ""
        document.updated_at = datetime.utcnow()
        session.add(document)
        session.commit()
        msg = f"已生成 {parsed_count} 条候选交易"
        if parse_warnings:
            msg += "。" + "；".join(parse_warnings)
        return msg


def fund_codes_for_source(session: Session, source_hash: str | None) -> set[str]:
    if not source_hash:
        return set()
    candidates = session.exec(
        select(FundTransactionCandidate).where(FundTransactionCandidate.source_hash == source_hash)
    ).all()
    return {
        item.fund_code
        for item in candidates
        if item.fund_code and item.fund_code != "000000"
    }


def sync_related_market_data(
    session: Session,
    fund_codes: set[str],
    *,
    sync_rules: bool = True,
    nav_pz: int = 40000,
) -> list[str]:
    errors = []
    for code in sorted(fund_codes):
        if sync_rules:
            try:
                existing_rule = session.get(FundRule, code)
                if not existing_rule or existing_rule.sync_source not in {"manual", "user"}:
                    synced = fetch_fund_rule_from_akshare(code)
                    rule = existing_rule or FundRule(fund_code=code)
                    rule.fund_name = synced.fund_name or rule.fund_name
                    if synced.buy_confirm_days is not None:
                        rule.buy_confirm_days = synced.buy_confirm_days
                    if synced.sell_confirm_days is not None:
                        rule.sell_confirm_days = synced.sell_confirm_days
                    rule.cutoff_time = synced.cutoff_time or rule.cutoff_time or "15:00"
                    if synced.buy_fee_rate is not None:
                        rule.buy_fee_rate = synced.buy_fee_rate
                    rule.sync_source = synced.source
                    rule.synced_at = sync_timestamp()
                    rule.fund_type = synced.fund_type or rule.fund_type
                    rule.updated_at = datetime.utcnow()
                    session.add(rule)
                    if synced.fee_tiers:
                        for tier in session.exec(select(FundFeeTier).where(FundFeeTier.fund_code == code)).all():
                            session.delete(tier)
                        for min_days, max_days, rate in synced.fee_tiers:
                            session.add(
                                FundFeeTier(
                                    fund_code=code,
                                    min_holding_days=min_days,
                                    max_holding_days=max_days,
                                    redemption_fee_rate=rate,
                                    updated_at=datetime.utcnow(),
                                )
                            )
                    session.commit()
                    if "货币" in (rule.fund_type or ""):
                        normalize_money_fund_records()
            except Exception as exc:
                errors.append(f"{code} 规则同步失败：{exc}")

        inserted, error = sync_nav_for_fund(session, code, pz=nav_pz)
        if error:
            errors.append(f"{code} 净值同步失败：{error}")

    inserted, error = sync_hs300(session)
    if error:
        errors.append(f"沪深300同步失败：{error}")
    return errors


def process_nav_job(payload: dict[str, Any]) -> str:
    fund_code = str(payload["fund_code"]).zfill(6)
    with Session(engine) as session:
        inserted, error = sync_nav_for_fund(session, fund_code)
        if error:
            raise RuntimeError(error)
        return f"{fund_code} 净值同步完成，新增 {inserted} 条"


def process_benchmark_job(payload: dict[str, Any]) -> str:
    with Session(engine) as session:
        inserted, error = sync_hs300(session)
        if error:
            raise RuntimeError(error)
        return f"沪深300同步完成，新增 {inserted} 条"


def process_daily_market_sync_job(payload: dict[str, Any]) -> str:
    with Session(engine) as session:
        fund_codes = current_holding_fund_codes(session)
        if not fund_codes:
            return "每日同步完成：当前无持仓基金"
        errors = sync_related_market_data(session, fund_codes, sync_rules=False, nav_pz=90)
        message = f"每日同步完成：持仓基金 {', '.join(sorted(fund_codes))}"
        if errors:
            message += "；" + "；".join(errors)
        return message


def current_holding_fund_codes(session: Session) -> set[str]:
    return {
        item.fund_code
        for item in calculate_position_summaries(session)
        if not item.is_closed and item.fund_code != "000000"
    }


def process_fund_rule_sync_job(payload: dict[str, Any]) -> str:
    code = str(payload["fund_code"]).zfill(6)
    synced = fetch_fund_rule_from_akshare(code)
    with Session(engine) as session:
        existing = session.get(FundRule, code)
        rule = existing or FundRule(fund_code=code)
        rule.fund_name = synced.fund_name or rule.fund_name
        if synced.buy_confirm_days is not None:
            rule.buy_confirm_days = synced.buy_confirm_days
        if synced.sell_confirm_days is not None:
            rule.sell_confirm_days = synced.sell_confirm_days
        rule.cutoff_time = synced.cutoff_time or rule.cutoff_time or "15:00"
        if synced.buy_fee_rate is not None:
            rule.buy_fee_rate = synced.buy_fee_rate
        rule.sync_source = synced.source
        rule.synced_at = sync_timestamp()
        rule.fund_type = synced.fund_type or rule.fund_type
        note_parts = [part for part in [rule.notes, synced.raw_notes] if part]
        rule.notes = "\n".join(dict.fromkeys(note_parts))
        rule.updated_at = datetime.utcnow()
        session.add(rule)

        if synced.fee_tiers:
            for tier in session.exec(select(FundFeeTier).where(FundFeeTier.fund_code == code)).all():
                session.delete(tier)
            for min_days, max_days, rate in synced.fee_tiers:
                session.add(
                    FundFeeTier(
                        fund_code=code,
                        min_holding_days=min_days,
                        max_holding_days=max_days,
                        redemption_fee_rate=rate,
                        updated_at=datetime.utcnow(),
                    )
                )
        session.commit()
        if "货币" in (rule.fund_type or ""):
            normalize_money_fund_records()
        return f"{code} 规则同步完成"


def process_auto_backup_job(payload: dict[str, Any]) -> str:
    with Session(engine) as session:
        path = write_backup_file(session)
        config = runtime_settings(session)
        keep = parse_int_value(config.get("AUTO_BACKUP_KEEP"), 30)
        prune_backup_files(keep)
        audit_log(session, "backup.auto", "backup", path.name, str(path))
        session.commit()
        return f"自动备份完成：{path.name}"


def backup_dir() -> Path:
    path = settings.data_dir / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_backup_file(session: Session) -> Path:
    payload = create_backup_payload(session)
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    path = backup_dir() / f"fund-ledger-backup-{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def prune_backup_files(keep: int) -> None:
    files = sorted(backup_dir().glob("fund-ledger-backup-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for path in files[max(keep, 1):]:
        path.unlink(missing_ok=True)


def backup_counts(payload: dict[str, Any]) -> dict[str, int]:
    return {
        key: len(payload.get(key) or [])
        for key in (
            "imports",
            "candidates",
            "transactions",
            "nav",
            "fund_rules",
            "fund_fee_tiers",
            "benchmark_nav",
            "aliases",
            "audits",
            "settings",
        )
    }


def restore_backup_payload(session: Session, payload: dict[str, Any]) -> dict[str, int]:
    if payload.get("version") != 1:
        raise ValueError("不支持的备份版本")
    counts = {key: 0 for key in backup_counts(payload)}

    for item in payload.get("settings") or []:
        if item.get("is_secret") and item.get("value") == "***":
            continue
        setting = session.get(AppSetting, item.get("key")) or AppSetting(key=item.get("key"))
        for key in ("value", "is_secret"):
            if key in item:
                setattr(setting, key, item[key])
        setting.updated_at = _parse_datetime_value(item.get("updated_at")) or datetime.utcnow()
        session.add(setting)
        counts["settings"] += 1

    for item in payload.get("fund_rules") or []:
        rule = session.get(FundRule, item.get("fund_code")) or FundRule(fund_code=item.get("fund_code"))
        _apply_fields(rule, item, {"fund_code"})
        session.add(rule)
        counts["fund_rules"] += 1

    for item in payload.get("imports") or []:
        existing = session.get(ImportDocument, item.get("id"))
        if existing:
            continue
        session.add(ImportDocument(**_model_data(item, {"created_at", "updated_at"})))
        counts["imports"] += 1

    for item in payload.get("candidates") or []:
        existing = session.get(FundTransactionCandidate, item.get("id"))
        if existing:
            continue
        session.add(FundTransactionCandidate(**_model_data(item, {"trade_date", "submitted_at", "confirm_date", "created_at", "updated_at"})))
        counts["candidates"] += 1

    for item in payload.get("transactions") or []:
        existing = session.get(FundTransaction, item.get("id"))
        if existing:
            continue
        session.add(FundTransaction(**_model_data(item, {"trade_date", "submitted_at", "confirm_date", "created_at"})))
        counts["transactions"] += 1

    for item in payload.get("fund_fee_tiers") or []:
        existing = session.get(FundFeeTier, item.get("id"))
        if existing:
            continue
        session.add(FundFeeTier(**_model_data(item, {"updated_at"})))
        counts["fund_fee_tiers"] += 1

    for item in payload.get("aliases") or []:
        existing = session.get(FundAlias, item.get("id"))
        if existing:
            continue
        session.add(FundAlias(**_model_data(item, {"created_at", "updated_at"})))
        counts["aliases"] += 1

    for item in payload.get("audits") or []:
        existing = session.get(OperationAudit, item.get("id"))
        if existing:
            continue
        session.add(OperationAudit(**_model_data(item, {"created_at"})))
        counts["audits"] += 1

    for item in payload.get("nav") or []:
        nav_date = parse_date_value(item.get("nav_date"))
        existing = session.exec(
            select(FundNav).where(
                FundNav.fund_code == item.get("fund_code"),
                FundNav.nav_date == nav_date,
            )
        ).first()
        if existing:
            _apply_fields(existing, item, {"id", "fund_code", "nav_date", "created_at"})
            existing.updated_at = datetime.utcnow()
            session.add(existing)
        else:
            session.add(FundNav(**_model_data(item, {"nav_date", "created_at", "updated_at"})))
        counts["nav"] += 1

    for item in payload.get("benchmark_nav") or []:
        nav_date = parse_date_value(item.get("nav_date"))
        existing = session.exec(
            select(BenchmarkNav).where(
                BenchmarkNav.benchmark_code == item.get("benchmark_code"),
                BenchmarkNav.nav_date == nav_date,
            )
        ).first()
        if existing:
            _apply_fields(existing, item, {"id", "benchmark_code", "nav_date", "created_at"})
            existing.updated_at = datetime.utcnow()
            session.add(existing)
        else:
            session.add(BenchmarkNav(**_model_data(item, {"nav_date", "created_at", "updated_at"})))
        counts["benchmark_nav"] += 1

    session.commit()
    return counts


def _model_data(item: dict[str, Any], typed_fields: set[str]) -> dict[str, Any]:
    data = dict(item)
    for key in typed_fields:
        if key not in data:
            continue
        if key == "submitted_at":
            data[key] = parse_optional_time_value(data[key])
        elif key.endswith("_date") or key == "nav_date":
            data[key] = parse_date_value(data[key])
        else:
            data[key] = _parse_datetime_value(data[key])
    return data


def _apply_fields(model: Any, item: dict[str, Any], skip: set[str]) -> None:
    for key, value in item.items():
        if key in skip or not hasattr(model, key):
            continue
        if key == "submitted_at":
            value = parse_optional_time_value(value)
        elif key.endswith("_date") or key == "nav_date":
            value = parse_date_value(value)
        elif key.endswith("_at"):
            value = _parse_datetime_value(value)
        setattr(model, key, value)


def _parse_datetime_value(value: Any) -> datetime | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


register_background_jobs()


@app.exception_handler(401)
async def auth_exception_handler(request: Request, exc: HTTPException):
    return redirect(f"/login?next={request.url.path}")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse("login.html", {"request": request, "next": next})


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    if not verify_login(username, password):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "next": next, "error": "用户名或密码不正确"},
            status_code=400,
        )
    login_user(request, username)
    return redirect(next or "/")


@app.post("/logout")
def logout(request: Request):
    logout_user(request)
    return redirect("/login")


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    holdings = calculate_holdings(session)
    latest_nav = session.exec(select(FundNav).order_by(desc(FundNav.nav_date))).first()
    pending_count = session.exec(
        select(FundTransactionCandidate).where(
            FundTransactionCandidate.status == CandidateStatus.pending
        )
    ).all()
    summary = {
        "total_value": sum(h.market_value for h in holdings),
        "total_cost": sum(h.cost for h in holdings),
        "total_profit": sum(h.profit for h in holdings),
        "fund_count": len(holdings),
        "latest_nav_date": latest_nav.nav_date if latest_nav else None,
        "pending_count": len(pending_count),
    }
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "summary": summary, "holdings": holdings[:5]},
    )


def build_health_report(session: Session) -> dict[str, Any]:
    today = date.today()
    stale_before = today - timedelta(days=7)
    candidates = session.exec(select(FundTransactionCandidate)).all()
    transactions = session.exec(select(FundTransaction)).all()
    imports = session.exec(select(ImportDocument)).all()
    rules = {rule.fund_code: rule for rule in session.exec(select(FundRule)).all()}
    jobs = session.exec(
        select(BackgroundJob).order_by(desc(BackgroundJob.created_at)).limit(20)
    ).all()
    latest_nav = {}
    for nav in session.exec(select(FundNav).order_by(FundNav.fund_code, desc(FundNav.nav_date))).all():
        latest_nav.setdefault(nav.fund_code, nav)
    positions = calculate_position_summaries(session)
    active_positions = [item for item in positions if not item.is_closed]
    known_codes = sorted(
        {
            item.fund_code
            for item in [*transactions, *candidates]
            if item.fund_code and item.fund_code != "000000"
        }
    )

    issues: list[dict[str, Any]] = []

    pending = [item for item in candidates if item.status == CandidateStatus.pending]
    if pending:
        issues.append(
            {
                "severity": "warn",
                "title": "候选交易待确认",
                "count": len(pending),
                "detail": "这些记录还没有进入正式流水，持仓和收益不会计算它们。",
                "link": "/candidates?status=pending",
                "action": "去确认",
            }
        )

    unmatched = [
        item
        for item in pending
        if item.fund_code == "000000" or not item.fund_name or item.fund_name in {"00", "未知基金", "未知"}
    ]
    if unmatched:
        issues.append(
            {
                "severity": "danger",
                "title": "候选交易基金未匹配",
                "count": len(unmatched),
                "detail": "这些记录的基金代码或名称不可靠，确认前需要先修正。",
                "link": "/candidates?status=pending&unmatched=1",
                "action": "去修正",
            }
        )

    low_confidence = [item for item in pending if (item.confidence or 0) < 0.75]
    if low_confidence:
        issues.append(
            {
                "severity": "warn",
                "title": "候选交易置信度偏低",
                "count": len(low_confidence),
                "detail": "建议人工核对金额、份额、净值和提交时间。",
                "link": "/candidates?status=pending&quality=review",
                "action": "去核对",
            }
        )

    failed_imports = [item for item in imports if item.status == ImportStatus.error]
    if failed_imports:
        issues.append(
            {
                "severity": "danger",
                "title": "导入文档失败",
                "count": len(failed_imports),
                "detail": "失败文档不会继续 OCR 或解析，需要重跑或查看错误。",
                "link": "/imports",
                "action": "看导入",
            }
        )

    failed_jobs = [item for item in jobs if item.status == JobStatus.error]
    if failed_jobs:
        issues.append(
            {
                "severity": "warn",
                "title": "后台任务失败",
                "count": len(failed_jobs),
                "detail": "最近后台任务里有失败项，可能影响 OCR、解析或净值同步。",
                "link": "/nav",
                "action": "看任务",
            }
        )

    missing_rules = [code for code in known_codes if code not in rules or not rules[code].fund_name]
    if missing_rules:
        issues.append(
            {
                "severity": "warn",
                "title": "基金规则不完整",
                "count": len(missing_rules),
                "detail": "缺少规则会影响确认日、申购费、赎回费和货币基金识别。",
                "link": "/fund-rules",
                "action": "同步规则",
                "codes": missing_rules[:8],
            }
        )

    qdii_auto_review_codes = qdii_rule_auto_review_codes(rules.values(), known_codes)
    if qdii_auto_review_codes:
        issues.append(
            {
                "severity": "info",
                "title": "QDII/海外基金规则可自动复核",
                "count": len(qdii_auto_review_codes),
                "detail": "系统会后台重新查询这些海外基金的确认日、费率和净值；只有查询失败或来源冲突时再提示处理。",
                "link": "/fund-rules",
                "action": "查看规则",
                "form_action": "/fund-rules/sync-qdiis",
                "form_label": "自动复核",
                "codes": qdii_auto_review_codes[:8],
            }
        )

    stale_nav_codes = []
    for item in active_positions:
        if is_money_fund(session, item.fund_code):
            continue
        nav = latest_nav.get(item.fund_code)
        if not nav or nav.nav_date < stale_before:
            stale_nav_codes.append(item.fund_code)
    if stale_nav_codes:
        issues.append(
            {
                "severity": "warn",
                "title": "当前持仓净值缺失或过旧",
                "count": len(stale_nav_codes),
                "detail": "净值过旧会让主界面收益和收益率不准。",
                "link": "/nav",
                "action": "同步净值",
                "codes": stale_nav_codes[:8],
            }
        )

    incomplete_transactions = []
    for tx in transactions:
        if tx.fund_code == "000000" or not tx.fund_name:
            incomplete_transactions.append(tx)
            continue
        if tx.action in {TransactionAction.buy, TransactionAction.sell, TransactionAction.dividend_reinvest}:
            if tx.share is None or tx.nav is None:
                incomplete_transactions.append(tx)
        elif tx.action == TransactionAction.dividend and tx.amount_cny is None:
            incomplete_transactions.append(tx)
    if incomplete_transactions:
        issues.append(
            {
                "severity": "danger",
                "title": "正式流水字段不完整",
                "count": len(incomplete_transactions),
                "detail": "正式流水缺字段会直接影响持仓、收益和曲线。",
                "link": "/transactions",
                "action": "修流水",
            }
        )

    severity_order = {"danger": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda item: (severity_order.get(item["severity"], 9), item["title"]))
    return {
        "issues": issues,
        "stats": {
            "transactions": len(transactions),
            "candidates_pending": len(pending),
            "imports_failed": len(failed_imports),
            "active_positions": len(active_positions),
            "known_funds": len(known_codes),
        },
        "ok": not issues,
        "checked_at": datetime.utcnow(),
    }


def is_qdii_like_rule(rule: FundRule) -> bool:
    text = f"{rule.fund_name or ''} {rule.fund_type or ''}".upper()
    if "港股通" in text and "QDII" not in text and "海外" not in text:
        return False
    return any(word in text for word in ("QDII", "海外", "全球", "纳斯达克", "标普", "美国"))


def qdii_rule_auto_review_codes(rules: list[FundRule] | Any, known_codes: list[str] | set[str]) -> list[str]:
    known = set(known_codes)
    codes = [
        rule.fund_code
        for rule in rules
        if rule.fund_code in known
        and is_qdii_like_rule(rule)
        and (not rule.sync_source or rule.buy_confirm_days <= 1 or rule.sell_confirm_days <= 1)
    ]
    return sorted(set(codes))


@app.get("/health", response_class=HTMLResponse)
def health_page(
    request: Request,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    report = build_health_report(session)
    return templates.TemplateResponse("health.html", {"request": request, "report": report})


def repair_preview(session: Session) -> dict[str, int]:
    candidates = session.exec(select(FundTransactionCandidate).where(FundTransactionCandidate.status == CandidateStatus.pending)).all()
    transactions = session.exec(select(FundTransaction)).all()
    return {
        "candidate_backfillable": sum(1 for item in candidates if _candidate_needs_value_backfill(item)),
        "transaction_backfillable": sum(1 for item in transactions if _transaction_needs_value_backfill(item)),
        "money_records": sum(
            1
            for item in [*candidates, *transactions]
            if item.fund_code != "000000" and is_money_fund(session, item.fund_code)
        ),
    }


@app.get("/repair", response_class=HTMLResponse)
def repair_page(
    request: Request,
    message: str = "",
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        "repair.html",
        {"request": request, "preview": repair_preview(session), "message": message},
    )


@app.post("/repair/run")
def repair_run(
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    tx_changed = backfill_incomplete_transaction_values(session)
    candidate_changed = 0
    for candidate in session.exec(
        select(FundTransactionCandidate).where(FundTransactionCandidate.status == CandidateStatus.pending)
    ).all():
        if backfill_candidate_values(session, candidate):
            candidate_changed += 1
    if candidate_changed:
        session.commit()
    money_changed = normalize_money_fund_records()
    audit_log(
        session,
        "repair.run",
        "system",
        detail=f"transactions={tx_changed}; candidates={candidate_changed}; money={money_changed}",
    )
    session.commit()
    return redirect(
        "/repair?message="
        + f"自动修复完成：流水 {tx_changed}，候选 {candidate_changed}，货币基金规范化 {money_changed}"
    )


def analytics_summary(session: Session) -> dict[str, Any]:
    positions = calculate_position_summaries(session)
    money_codes = money_fund_codes(session)
    txs = session.exec(select(FundTransaction).order_by(FundTransaction.trade_date)).all()
    monthly: dict[str, float] = {}
    dividends = 0.0
    for tx in txs:
        if tx.fund_code in money_codes:
            continue
        key = tx.trade_date.strftime("%Y-%m")
        if tx.action == TransactionAction.buy:
            monthly[key] = monthly.get(key, 0.0) + (tx.amount_cny or 0.0) + (tx.fee or 0.0)
        elif tx.action == TransactionAction.dividend:
            dividends += tx.amount_cny or 0.0
    closed = [item for item in positions if item.is_closed]
    active = [item for item in positions if not item.is_closed]
    return {
        "active": active,
        "closed": sorted(closed, key=lambda item: item.realized_profit, reverse=True),
        "monthly": sorted(monthly.items()),
        "total_buy": sum(item.total_buy_amount for item in positions),
        "total_sell": sum(item.total_sell_amount for item in positions),
        "total_dividend": dividends,
        "closed_profit": sum(item.realized_profit for item in closed),
        "active_profit": sum(item.profit for item in active),
    }


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(
    request: Request,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        "analytics.html",
        {"request": request, "summary": analytics_summary(session)},
    )


@app.get("/aliases", response_class=HTMLResponse)
def aliases_page(
    request: Request,
    message: str = "",
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    aliases = session.exec(select(FundAlias).order_by(FundAlias.id)).all()
    return templates.TemplateResponse(
        "aliases.html",
        {
            "request": request,
            "aliases": aliases,
            "defaults": DEFAULT_FUND_ALIASES,
            "message": message,
        },
    )


@app.post("/aliases")
def alias_create(
    pattern: str = Form(...),
    replacement: str = Form(""),
    notes: str = Form(""),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    alias = FundAlias(pattern=pattern.strip(), replacement=replacement.strip(), notes=notes.strip())
    session.add(alias)
    session.flush()
    audit_log(session, "alias.create", "fund_alias", str(alias.id), f"{alias.pattern} -> {alias.replacement}")
    session.commit()
    return redirect("/aliases?message=别名规则已添加")


@app.post("/aliases/{alias_id}/delete")
def alias_delete(
    alias_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    alias = session.get(FundAlias, alias_id)
    if not alias:
        raise HTTPException(status_code=404)
    detail = f"{alias.pattern} -> {alias.replacement}"
    session.delete(alias)
    audit_log(session, "alias.delete", "fund_alias", str(alias_id), detail)
    session.commit()
    return redirect("/aliases?message=别名规则已删除")


@app.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    rows = session.exec(select(OperationAudit).order_by(desc(OperationAudit.created_at)).limit(100)).all()
    return templates.TemplateResponse("audit.html", {"request": request, "rows": rows})


@app.head("/")
def dashboard_head(request: Request):
    if not current_user(request):
        return redirect("/login?next=/")
    return Response(status_code=200)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    message: str = "",
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    config = runtime_settings(session)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "config": config,
            "message": message,
            "masked": masked,
            "configured": configured,
        },
    )


@app.post("/settings")
def settings_update(
    deepseek_enabled: str = Form("off"),
    deepseek_api_key: str = Form(""),
    deepseek_base_url: str = Form("https://api.deepseek.com"),
    deepseek_model: str = Form("deepseek-chat"),
    ocr_enabled: str = Form("off"),
    ocr_backend: str = Form("rapidocr"),
    ocr_api_provider: str = Form("generic"),
    ocr_api_url: str = Form(""),
    ocr_api_auth_header: str = Form("Authorization"),
    ocr_api_auth_prefix: str = Form("Bearer "),
    ocr_api_key: str = Form(""),
    ocr_api_file_field: str = Form("file"),
    ocr_api_text_path: str = Form("text"),
    baidu_ocr_api_key: str = Form(""),
    baidu_ocr_secret_key: str = Form(""),
    baidu_ocr_endpoint: str = Form("https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic"),
    auto_market_sync_enabled: str = Form("off"),
    auto_market_sync_time: str = Form("21:30"),
    auto_market_sync_timezone: str = Form("Asia/Shanghai"),
    auto_backup_enabled: str = Form("off"),
    auto_backup_time: str = Form("02:10"),
    auto_backup_timezone: str = Form("Asia/Shanghai"),
    auto_backup_keep: str = Form("30"),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    current = runtime_settings(session)
    values = {
        "DEEPSEEK_ENABLED": "true" if deepseek_enabled == "on" else "false",
        "DEEPSEEK_API_KEY": deepseek_api_key.strip() or current.get("DEEPSEEK_API_KEY", ""),
        "DEEPSEEK_BASE_URL": deepseek_base_url.strip() or "https://api.deepseek.com",
        "DEEPSEEK_MODEL": deepseek_model.strip() or "deepseek-chat",
        "OCR_ENABLED": "true" if ocr_enabled == "on" else "false",
        "OCR_BACKEND": ocr_backend,
        "OCR_API_PROVIDER": ocr_api_provider,
        "OCR_API_URL": ocr_api_url.strip(),
        "OCR_API_AUTH_HEADER": ocr_api_auth_header.strip() or "Authorization",
        "OCR_API_AUTH_PREFIX": ocr_api_auth_prefix,
        "OCR_API_KEY": ocr_api_key.strip() or current.get("OCR_API_KEY", ""),
        "OCR_API_FILE_FIELD": ocr_api_file_field.strip() or "file",
        "OCR_API_TEXT_PATH": ocr_api_text_path.strip() or "text",
        "BAIDU_OCR_API_KEY": baidu_ocr_api_key.strip() or current.get("BAIDU_OCR_API_KEY", ""),
        "BAIDU_OCR_SECRET_KEY": baidu_ocr_secret_key.strip()
        or current.get("BAIDU_OCR_SECRET_KEY", ""),
        "BAIDU_OCR_ENDPOINT": baidu_ocr_endpoint.strip()
        or "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic",
        "AUTO_MARKET_SYNC_ENABLED": "true" if auto_market_sync_enabled == "on" else "false",
        "AUTO_MARKET_SYNC_TIME": auto_market_sync_time.strip() or "21:30",
        "AUTO_MARKET_SYNC_TIMEZONE": auto_market_sync_timezone.strip() or "Asia/Shanghai",
        "AUTO_BACKUP_ENABLED": "true" if auto_backup_enabled == "on" else "false",
        "AUTO_BACKUP_TIME": auto_backup_time.strip() or "02:10",
        "AUTO_BACKUP_TIMEZONE": auto_backup_timezone.strip() or "Asia/Shanghai",
        "AUTO_BACKUP_KEEP": str(max(parse_int_value(auto_backup_keep, 30), 1)),
    }
    save_settings(session, values)
    return redirect("/settings?message=设置已保存")


@app.get("/fund-rules", response_class=HTMLResponse)
def fund_rules_page(
    request: Request,
    message: str = "",
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    rules = session.exec(select(FundRule).order_by(FundRule.fund_code)).all()
    tiers = session.exec(select(FundFeeTier).order_by(FundFeeTier.fund_code, FundFeeTier.min_holding_days)).all()
    jobs = session.exec(
        select(BackgroundJob)
        .where(BackgroundJob.job_type == "sync_fund_rule")
        .order_by(desc(BackgroundJob.created_at))
        .limit(5)
    ).all()
    tiers_by_code: dict[str, list[FundFeeTier]] = {}
    for tier in tiers:
        tiers_by_code.setdefault(tier.fund_code, []).append(tier)
    known_codes = {
        item.fund_code
        for item in session.exec(select(FundTransaction)).all()
        if item.fund_code and item.fund_code != "000000"
    } | {
        item.fund_code
        for item in session.exec(select(FundTransactionCandidate)).all()
        if item.fund_code and item.fund_code != "000000"
    }
    qdii_review_codes = qdii_rule_auto_review_codes(rules, known_codes)
    return templates.TemplateResponse(
        "fund_rules.html",
        {
            "request": request,
            "rules": rules,
            "tiers_by_code": tiers_by_code,
            "message": message,
            "jobs": jobs,
            "qdii_review_codes": qdii_review_codes,
        },
    )


@app.post("/fund-rules")
def fund_rule_save(
    fund_code: str = Form(...),
    fund_name: str = Form(""),
    buy_confirm_days: int = Form(1),
    sell_confirm_days: int = Form(1),
    cutoff_time: str = Form("15:00"),
    buy_fee_rate: float = Form(0.0),
    notes: str = Form(""),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    code = fund_code.zfill(6)
    rule = session.get(FundRule, code) or FundRule(fund_code=code)
    rule.fund_name = fund_name
    rule.buy_confirm_days = max(buy_confirm_days, 0)
    rule.sell_confirm_days = max(sell_confirm_days, 0)
    rule.cutoff_time = cutoff_time or "15:00"
    rule.buy_fee_rate = max(buy_fee_rate, 0.0)
    rule.notes = notes
    rule.sync_source = rule.sync_source or "manual"
    rule.updated_at = datetime.utcnow()
    session.add(rule)
    session.commit()
    return redirect("/fund-rules?message=规则已保存")


@app.post("/fund-rules/sync")
def fund_rule_sync(
    fund_code: str = Form(...),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    code = fund_code.zfill(6)
    job = create_and_enqueue(session, "sync_fund_rule", {"fund_code": code})
    return redirect(f"/fund-rules?message=规则同步已加入后台任务 #{job.id}")


@app.post("/fund-rules/sync-qdiis")
def fund_rule_sync_qdiis(
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    rules = session.exec(select(FundRule)).all()
    known_codes = {
        item.fund_code
        for item in session.exec(select(FundTransaction)).all()
        if item.fund_code and item.fund_code != "000000"
    } | {
        item.fund_code
        for item in session.exec(select(FundTransactionCandidate)).all()
        if item.fund_code and item.fund_code != "000000"
    }
    codes = qdii_rule_auto_review_codes(rules, known_codes)
    jobs = [create_and_enqueue(session, "sync_fund_rule", {"fund_code": code}) for code in codes]
    if not jobs:
        return redirect("/fund-rules?message=暂无需要自动复核的 QDII/海外基金规则")
    return redirect(f"/fund-rules?message=已加入 {len(jobs)} 个 QDII/海外规则复核任务")


@app.post("/fund-rules/{fund_code}/tiers")
def fund_fee_tier_add(
    fund_code: str,
    min_holding_days: str = Form("0"),
    max_holding_days: str = Form(""),
    redemption_fee_rate: str = Form("0"),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    code = fund_code.zfill(6)
    if not session.get(FundRule, code):
        session.add(FundRule(fund_code=code))
    session.add(
        FundFeeTier(
            fund_code=code,
            min_holding_days=max(parse_int_value(min_holding_days) or 0, 0),
            max_holding_days=parse_positive_int_value(max_holding_days),
            redemption_fee_rate=max(parse_float_value(redemption_fee_rate) or 0.0, 0.0),
            updated_at=datetime.utcnow(),
        )
    )
    session.commit()
    return redirect("/fund-rules?message=费率档已添加")


@app.post("/fund-rules/tiers/{tier_id}/delete")
def fund_fee_tier_delete(
    tier_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    tier = session.get(FundFeeTier, tier_id)
    if not tier:
        raise HTTPException(status_code=404)
    session.delete(tier)
    session.commit()
    return redirect("/fund-rules?message=费率档已删除")


@app.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, _: str = Depends(require_user)):
    return templates.TemplateResponse("upload.html", {"request": request})


@app.post("/upload")
async def upload_submit(
    request: Request,
    raw_text: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    file: Optional[UploadFile] = File(None),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    ensure_data_dirs()
    uploaded_files = [item for item in files if item and item.filename]
    if file and file.filename:
        uploaded_files.append(file)

    documents: list[ImportDocument] = []
    if uploaded_files:
        for upload in uploaded_files:
            try:
                document = await create_import_document_from_upload(session, upload, raw_text)
                documents.append(document)
            except HTTPException as exc:
                return redirect(f"/upload?message={exc.detail}")
    else:
        document = create_import_document_from_text(session, raw_text)
        documents.append(document)

    job_ids = []
    for document in documents:
        job = create_and_enqueue(session, "auto_import", {"document_id": document.id})
        job_ids.append(str(job.id))
    if len(documents) == 1:
        return redirect(f"/imports/{documents[0].id}?message=自动导入已加入后台任务 #{job_ids[0]}")
    return redirect(f"/imports?message=已上传 {len(documents)} 个文件，自动导入任务 #{', #'.join(job_ids)}")


async def create_import_document_from_upload(
    session: Session,
    upload: UploadFile,
    raw_text: str = "",
) -> ImportDocument:
    content = await upload.read()
    safe_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}-{Path(upload.filename).name}"
    target = settings.uploads_dir / safe_name
    target.write_bytes(content)
    source_hash = hash_file(target)

    existing = session.exec(
        select(ImportDocument).where(
            ImportDocument.file_name == upload.filename,
            ImportDocument.status.notin_([ImportStatus.deleted]),
        )
    ).first()
    if existing:
        target.unlink(missing_ok=True)
        raise HTTPException(
            status_code=409,
            detail=f"文件「{upload.filename}」已存在（导入 #{existing.id}），请勿重复上传",
        )

    document = ImportDocument(
        raw_text=raw_text,
        file_name=upload.filename,
        source_file=str(target),
        source_hash=source_hash,
        content_type=upload.content_type,
        status=ImportStatus.uploaded,
    )
    session.add(document)
    session.commit()
    session.refresh(document)
    return document


def create_import_document_from_text(session: Session, raw_text: str) -> ImportDocument:
    source_hash = hash_content(raw_text.encode())
    document = ImportDocument(
        raw_text=raw_text,
        source_hash=source_hash,
        status=ImportStatus.uploaded,
    )
    session.add(document)
    session.commit()
    session.refresh(document)
    return document


@app.get("/imports", response_class=HTMLResponse)
def imports_page(
    request: Request,
    show: str = "",
    message: str = "",
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    query = select(ImportDocument).order_by(desc(ImportDocument.created_at))
    if show != "all":
        query = query.where(ImportDocument.status != ImportStatus.deleted)
    documents = session.exec(query).all()
    config = runtime_settings(session)
    import_audits = import_audits_for_documents(session, documents)
    import_summary = summarize_imports(documents, import_audits)
    return templates.TemplateResponse(
        "imports.html",
        {
            "request": request,
            "documents": documents,
            "audits": import_audits,
            "summary": import_summary,
            "show": show,
            "message": message,
            "llm_configured": is_deepseek_configured(config),
        },
    )


@app.post("/imports/retry-failed")
def imports_retry_failed(
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    documents = session.exec(
        select(ImportDocument)
        .where(ImportDocument.status == ImportStatus.error)
        .order_by(ImportDocument.created_at)
    ).all()
    job_ids = []
    cleared = {"transactions": 0, "candidates": 0}
    for document in documents:
        job, cleared_count = retry_import_document(session, document)
        job_ids.append(str(job.id))
        cleared["transactions"] += cleared_count["transactions"]
        cleared["candidates"] += cleared_count["candidates"]
    if not documents:
        return redirect("/imports?message=没有失败导入需要重跑")
    message = f"已重跑 {len(documents)} 个失败导入，后台任务 #{', #'.join(job_ids)}"
    if cleared["transactions"] or cleared["candidates"]:
        message += f"，已失效旧流水 {cleared['transactions']} 条、旧候选 {cleared['candidates']} 条"
    return redirect(f"/imports?message={message}")


@app.get("/imports/{document_id}", response_class=HTMLResponse)
def import_detail_page(
    document_id: int,
    request: Request,
    message: str = "",
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    document = session.get(ImportDocument, document_id)
    if not document:
        raise HTTPException(status_code=404)
    config = runtime_settings(session)
    jobs = session.exec(
        select(BackgroundJob)
        .where(BackgroundJob.payload_json.contains(f'"document_id": {document_id}'))
        .order_by(desc(BackgroundJob.created_at))
        .limit(5)
    ).all()
    audit = import_audit(session, document)
    return templates.TemplateResponse(
        "import_detail.html",
        {
            "request": request,
            "document": document,
            "audit": audit,
            "message": message,
            "llm_configured": is_deepseek_configured(config),
            "ocr_backend": config.get("OCR_BACKEND", "rapidocr"),
            "jobs": jobs,
        },
    )


@app.post("/imports/{document_id}/archive")
def import_archive(
    document_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    document = session.get(ImportDocument, document_id)
    if not document:
        raise HTTPException(status_code=404)
    document.status = ImportStatus.archived
    document.updated_at = datetime.utcnow()
    session.add(document)
    session.commit()
    return redirect("/imports")


@app.post("/imports/{document_id}/restore")
def import_restore(
    document_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    document = session.get(ImportDocument, document_id)
    if not document:
        raise HTTPException(status_code=404)
    document.status = ImportStatus.uploaded
    document.updated_at = datetime.utcnow()
    session.add(document)
    session.commit()
    return redirect(f"/imports/{document_id}")


@app.post("/imports/{document_id}/delete")
def import_delete(
    document_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    document = session.get(ImportDocument, document_id)
    if not document:
        raise HTTPException(status_code=404)
    invalidated = invalidate_import_results(session, document)
    if document.source_file:
        path = Path(document.source_file)
        if path.exists() and path.is_file():
            path.unlink()
    document.status = ImportStatus.deleted
    document.source_file = None
    document.updated_at = datetime.utcnow()
    session.add(document)
    session.commit()
    return redirect(
        "/imports?message="
        + f"导入已删除，已失效旧流水 {invalidated['transactions']} 条、旧候选 {invalidated['candidates']} 条"
    )


@app.post("/imports/{document_id}/ocr")
def import_run_ocr(
    document_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    document = session.get(ImportDocument, document_id)
    if not document:
        raise HTTPException(status_code=404)
    if not document.source_file:
        return redirect(f"/imports/{document_id}?message=没有可 OCR 的文件")
    invalidated = invalidate_import_results(session, document)
    document.status = ImportStatus.ocr_running
    document.ocr_text = ""
    document.llm_text = ""
    document.error_message = ""
    document.updated_at = datetime.utcnow()
    session.add(document)
    session.commit()
    job = create_and_enqueue(session, "ocr_import", {"document_id": document_id})
    return redirect(
        f"/imports/{document_id}?message=OCR 已加入后台任务 #{job.id}，"
        f"已失效旧流水 {invalidated['transactions']} 条、旧候选 {invalidated['candidates']} 条"
    )


@app.post("/imports/{document_id}/text")
def import_update_text(
    document_id: int,
    raw_text: str = Form(""),
    ocr_text: str = Form(""),
    llm_text: str = Form(""),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    document = session.get(ImportDocument, document_id)
    if not document:
        raise HTTPException(status_code=404)
    document.raw_text = raw_text
    document.ocr_text = ocr_text
    document.llm_text = llm_text
    document.updated_at = datetime.utcnow()
    session.add(document)
    session.commit()
    return redirect(f"/imports/{document_id}?message=文本已保存")


@app.post("/imports/{document_id}/parse")
def import_parse(
    document_id: int,
    use_llm: bool = Form(False),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    document = session.get(ImportDocument, document_id)
    if not document:
        raise HTTPException(status_code=404)
    text = document.ocr_text or document.raw_text
    if not text.strip():
        return redirect(f"/imports/{document_id}?message=没有可解析文本")
    invalidated = invalidate_import_results(session, document)
    document.error_message = ""
    session.add(document)
    session.commit()
    job = create_and_enqueue(session, "parse_import", {"document_id": document_id, "use_llm": use_llm})
    return redirect(
        f"/imports/{document_id}?message=解析已加入后台任务 #{job.id}，"
        f"已失效旧流水 {invalidated['transactions']} 条、旧候选 {invalidated['candidates']} 条"
    )


@app.post("/imports/{document_id}/retry")
def import_retry(
    document_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    document = session.get(ImportDocument, document_id)
    if not document:
        raise HTTPException(status_code=404)
    job, cleared = retry_import_document(session, document)
    message = f"自动导入已重新加入后台任务 #{job.id}"
    if cleared["transactions"] or cleared["candidates"]:
        message += f"，已失效旧流水 {cleared['transactions']} 条、旧候选 {cleared['candidates']} 条"
    return redirect(f"/imports/{document_id}?message={message}")


def retry_import_document(session: Session, document: ImportDocument) -> tuple[BackgroundJob, dict[str, int]]:
    cleared = invalidate_import_results(session, document)
    document.error_message = ""
    if document.source_file:
        document.ocr_text = ""
    document.llm_text = ""
    document.status = ImportStatus.uploaded if document.source_file else ImportStatus.ocr_done
    document.updated_at = datetime.utcnow()
    session.add(document)
    session.commit()
    job = create_and_enqueue(session, "auto_import", {"document_id": document.id})
    return job, cleared


def import_audit(session: Session, document: ImportDocument) -> dict[str, int]:
    candidates = []
    if document.source_hash:
        candidates = session.exec(
            select(FundTransactionCandidate).where(FundTransactionCandidate.source_hash == document.source_hash)
        ).all()
    transactions = []
    if document.source_file:
        transactions = session.exec(
            select(FundTransaction).where(FundTransaction.source_file == document.source_file)
        ).all()
    low_confidence = sum(1 for item in candidates if (item.confidence or 0) < 0.75)
    confirmed = sum(1 for item in candidates if item.status == CandidateStatus.confirmed)
    total = len(candidates)
    return {
        "candidates": len(candidates),
        "pending": sum(1 for item in candidates if item.status == CandidateStatus.pending),
        "confirmed": confirmed,
        "ignored": sum(1 for item in candidates if item.status == CandidateStatus.ignored),
        "unmatched": sum(1 for item in candidates if item.fund_code == "000000"),
        "transactions": len(transactions),
        "duplicate_candidates": count_duplicate_candidate_groups(candidates),
        "auto_confirmable": sum(1 for item in candidates if candidate_quality(session, item)["auto_confirmable"]),
        "low_confidence": low_confidence,
        "success_rate": round(confirmed / total * 100) if total else 0,
    }


def import_audits_for_documents(
    session: Session,
    documents: list[ImportDocument],
) -> dict[int, dict[str, int]]:
    source_hashes = [doc.source_hash for doc in documents if doc.id is not None and doc.source_hash]
    source_files = [doc.source_file for doc in documents if doc.id is not None and doc.source_file]
    candidates_by_hash: dict[str, list[FundTransactionCandidate]] = {}
    transactions_by_file: dict[str, int] = {}
    if source_hashes:
        candidates = session.exec(
            select(FundTransactionCandidate).where(FundTransactionCandidate.source_hash.in_(source_hashes))
        ).all()
        for candidate in candidates:
            if candidate.source_hash:
                candidates_by_hash.setdefault(candidate.source_hash, []).append(candidate)
    if source_files:
        transactions = session.exec(
            select(FundTransaction).where(FundTransaction.source_file.in_(source_files))
        ).all()
        for transaction in transactions:
            if transaction.source_file:
                transactions_by_file[transaction.source_file] = transactions_by_file.get(transaction.source_file, 0) + 1
    audits: dict[int, dict[str, int]] = {}
    for document in documents:
        candidates = candidates_by_hash.get(document.source_hash or "", [])
        audits[document.id or 0] = {
            "candidates": len(candidates),
            "pending": sum(1 for item in candidates if item.status == CandidateStatus.pending),
            "confirmed": sum(1 for item in candidates if item.status == CandidateStatus.confirmed),
            "ignored": sum(1 for item in candidates if item.status == CandidateStatus.ignored),
            "unmatched": sum(1 for item in candidates if item.fund_code == "000000"),
            "transactions": transactions_by_file.get(document.source_file or "", 0),
            "duplicate_candidates": count_duplicate_candidate_groups(candidates),
            "auto_confirmable": sum(1 for item in candidates if candidate_quality(session, item)["auto_confirmable"]),
            "low_confidence": sum(1 for item in candidates if (item.confidence or 0) < 0.75),
            "success_rate": round(
                sum(1 for item in candidates if item.status == CandidateStatus.confirmed) / len(candidates) * 100
            ) if candidates else 0,
        }
    return audits


def summarize_imports(
    documents: list[ImportDocument],
    audits: dict[int, dict[str, int]],
) -> dict[str, int]:
    return {
        "documents": len(documents),
        "failed": sum(1 for document in documents if document.status == ImportStatus.error),
        "running": sum(1 for document in documents if document.status in {ImportStatus.uploaded, ImportStatus.ocr_running}),
        "pending": sum(audit["pending"] for audit in audits.values()),
        "unmatched": sum(audit["unmatched"] for audit in audits.values()),
        "transactions": sum(audit["transactions"] for audit in audits.values()),
        "auto_confirmable": sum(audit["auto_confirmable"] for audit in audits.values()),
        "low_confidence": sum(audit["low_confidence"] for audit in audits.values()),
    }


def invalidate_import_results(session: Session, document: ImportDocument) -> dict[str, int]:
    candidates = []
    if document.source_hash:
        candidates = session.exec(
            select(FundTransactionCandidate).where(FundTransactionCandidate.source_hash == document.source_hash)
        ).all()
    candidate_ids = [candidate.id for candidate in candidates if candidate.id]
    transactions = []
    if document.source_file:
        transactions = session.exec(
            select(FundTransaction).where(FundTransaction.source_file == document.source_file)
        ).all()
    if candidate_ids:
        linked_transactions = session.exec(
            select(FundTransaction).where(FundTransaction.candidate_id.in_(candidate_ids))
        ).all()
        existing_tx_ids = {transaction.id for transaction in transactions}
        transactions.extend(transaction for transaction in linked_transactions if transaction.id not in existing_tx_ids)
        linked_candidates = session.exec(
            select(FundTransactionCandidate).where(FundTransactionCandidate.id.in_(candidate_ids))
        ).all()
        existing_ids = {candidate.id for candidate in candidates}
        candidates.extend(candidate for candidate in linked_candidates if candidate.id not in existing_ids)
    for transaction in transactions:
        session.delete(transaction)
    for candidate in candidates:
        session.delete(candidate)
    session.commit()
    return {"transactions": len(transactions), "candidates": len(candidates)}


def count_duplicate_candidate_groups(candidates: list[FundTransactionCandidate]) -> int:
    groups: dict[tuple, int] = {}
    for candidate in candidates:
        key = candidate_duplicate_key(candidate)
        if key:
            groups[key] = groups.get(key, 0) + 1
    return sum(1 for count in groups.values() if count > 1)


@app.get("/candidates", response_class=HTMLResponse)
def candidates_page(
    request: Request,
    source_hash: str = "",
    status: str = "",
    unmatched: bool = False,
    quality: str = "",
    message: str = "",
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    query = select(FundTransactionCandidate)
    if source_hash:
        query = query.where(FundTransactionCandidate.source_hash == source_hash)
    if status in {item.value for item in CandidateStatus}:
        query = query.where(FundTransactionCandidate.status == CandidateStatus(status))
    candidates = session.exec(
        query.order_by(FundTransactionCandidate.status, desc(FundTransactionCandidate.created_at))
    ).all()
    quality_by_candidate = {c.id: candidate_quality(session, c) for c in candidates if c.id is not None}
    if quality == "auto":
        candidates = [c for c in candidates if quality_by_candidate.get(c.id, {}).get("auto_confirmable")]
    elif quality == "low":
        candidates = [c for c in candidates if quality_by_candidate.get(c.id, {}).get("label") == "低"]
    elif quality == "review":
        candidates = [
            c
            for c in candidates
            if c.status == CandidateStatus.pending
            and not quality_by_candidate.get(c.id, {}).get("auto_confirmable")
        ]
    matched = [c for c in candidates if c.fund_code != "000000"]
    unmatched_candidates = [c for c in candidates if c.fund_code == "000000" and c.status == CandidateStatus.pending]
    if unmatched:
        matched = []
    selected_document = None
    if source_hash:
        selected_document = session.exec(
            select(ImportDocument).where(ImportDocument.source_hash == source_hash)
        ).first()
    groups: dict[str, list] = {}
    for c in unmatched_candidates:
        key = c.fund_name or c.raw_text[:30]
        groups.setdefault(key, []).append(c)
    unmatched_groups = []
    for name, items in sorted(groups.items(), key=lambda x: -len(x[1])):
        samples = "; ".join(set(c.raw_text[:40] for c in items))[:80]
        unmatched_groups.append((name, len(items), samples))
    duplicate_candidate_ids = duplicate_candidate_warning_ids(session, matched)
    return templates.TemplateResponse(
        "candidates.html",
        {
            "request": request,
            "matched": matched,
            "unmatched": unmatched_candidates,
            "unmatched_groups": unmatched_groups,
            "duplicate_candidate_ids": duplicate_candidate_ids,
            "quality_by_candidate": quality_by_candidate,
            "actions": list(TransactionAction),
            "filters": {
                "source_hash": source_hash,
                "status": status,
                "unmatched": bool(unmatched),
                "quality": quality,
            },
            "return_to": candidates_url(source_hash, status, bool(unmatched), quality),
            "selected_document": selected_document,
            "message": message,
        },
    )


@app.post("/candidates/{candidate_id}/update")
def candidate_update(
    candidate_id: int,
    fund_code: str = Form(...),
    fund_name: str = Form(""),
    trade_date: date = Form(...),
    submitted_at: Optional[time] = Form(None),
    confirm_date: Optional[date] = Form(None),
    action: TransactionAction = Form(...),
    amount_cny: Optional[float] = Form(None),
    share: Optional[float] = Form(None),
    nav: Optional[float] = Form(None),
    fee: Optional[float] = Form(None),
    return_to: str = Form("/candidates"),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    candidate = session.get(FundTransactionCandidate, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404)
    candidate.fund_code = fund_code.zfill(6)
    candidate.fund_name = fund_name
    candidate.trade_date = trade_date
    candidate.submitted_at = submitted_at
    candidate.action = action
    calc_amount, calc_share, calc_fee, calc_confirm, calc_trade_date = apply_trade_calculation(
        session,
        fund_code.zfill(6),
        action,
        amount_cny,
        share,
        nav,
        trade_date,
        submitted_at=submitted_at,
    )
    candidate.amount_cny = amount_cny if amount_cny is not None else calc_amount
    candidate.share = share if share is not None else calc_share
    candidate.nav = 1.0 if is_money_fund(session, fund_code.zfill(6)) else (
        nav if nav is not None else nav_value_on_date(session, fund_code.zfill(6), calc_trade_date)
    )
    candidate.fee = fee if fee is not None else calc_fee
    candidate.confirm_date = confirm_date or calc_confirm
    candidate.trade_date = trade_date if confirm_date else calc_trade_date
    candidate.updated_at = datetime.utcnow()
    session.add(candidate)
    session.commit()
    return redirect(safe_candidates_return(return_to))


@app.post("/candidates/{candidate_id}/confirm")
def candidate_confirm(
    candidate_id: int,
    return_to: str = Form("/candidates"),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    candidate = session.get(FundTransactionCandidate, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404)
    if candidate.status == CandidateStatus.confirmed:
        return redirect(safe_candidates_return(return_to))
    confirm_candidate_transaction(session, candidate)
    session.commit()
    return redirect(safe_candidates_return(return_to))


@app.post("/candidates/{candidate_id}/ignore")
def candidate_ignore(
    candidate_id: int,
    return_to: str = Form("/candidates"),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    candidate = session.get(FundTransactionCandidate, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404)
    if candidate.status == CandidateStatus.ignored:
        candidate.status = CandidateStatus.pending
    elif candidate.status != CandidateStatus.confirmed:
        candidate.status = CandidateStatus.ignored
    candidate.updated_at = datetime.utcnow()
    session.add(candidate)
    session.commit()
    return redirect(safe_candidates_return(return_to))


@app.post("/candidates/confirm-all")
def candidates_confirm_all(
    source_hash: str = Form(""),
    return_to: str = Form("/candidates"),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    query = select(FundTransactionCandidate).where(
        FundTransactionCandidate.status == CandidateStatus.pending,
        FundTransactionCandidate.fund_code != "000000",
    )
    if source_hash:
        query = query.where(FundTransactionCandidate.source_hash == source_hash)
    candidates = session.exec(query).all()
    confirmed = 0
    for candidate in candidates:
        if confirm_candidate_transaction(session, candidate):
            confirmed += 1
    session.commit()
    skipped = len(candidates) - confirmed
    msg = f"已确认 {confirmed} 条"
    if skipped:
        msg += f"，跳过 {skipped} 条（重复）"
    return candidates_redirect_with_message(return_to, msg)


@app.post("/candidates/auto-confirm-safe")
def candidates_auto_confirm_safe(
    source_hash: str = Form(""),
    return_to: str = Form("/candidates"),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    query = select(FundTransactionCandidate).where(FundTransactionCandidate.status == CandidateStatus.pending)
    if source_hash:
        query = query.where(FundTransactionCandidate.source_hash == source_hash)
    candidates = session.exec(query).all()
    confirmed = 0
    skipped = 0
    for candidate in candidates:
        quality = candidate_quality(session, candidate)
        if not quality["auto_confirmable"]:
            skipped += 1
            continue
        if confirm_candidate_transaction(session, candidate):
            confirmed += 1
        else:
            skipped += 1
    session.commit()
    return candidates_redirect_with_message(return_to, f"已自动确认 {confirmed} 条高质量候选，保留 {skipped} 条待人工核对")


def confirm_candidate_transaction(session: Session, candidate: FundTransactionCandidate) -> bool:
    if candidate.status == CandidateStatus.confirmed:
        return False
    if infer_candidate_status(candidate.raw_text) == CandidateStatus.ignored:
        candidate.status = CandidateStatus.ignored
        candidate.updated_at = datetime.utcnow()
        session.add(candidate)
        return False
    backfill_candidate_values(session, candidate)
    existing = session.exec(select(FundTransaction).where(FundTransaction.candidate_id == candidate.id)).first()
    if existing:
        candidate.status = CandidateStatus.confirmed
        candidate.confirmed_transaction_id = existing.id
        candidate.updated_at = datetime.utcnow()
        session.add(candidate)
        return False
    duplicate = find_duplicate_transaction(session, candidate)
    if duplicate:
        candidate.status = CandidateStatus.confirmed
        candidate.confirmed_transaction_id = duplicate.id
        candidate.updated_at = datetime.utcnow()
        session.add(candidate)
        return False
    fee = candidate.fee
    if candidate.action == TransactionAction.sell and candidate.share and candidate.nav and fee is None:
        money = is_money_fund(session, candidate.fund_code)
        fee = 0.0 if money else infer_redemption_fee(
            session,
            candidate.fund_code,
            candidate.share,
            candidate.nav,
            candidate.trade_date,
        )
    tx = FundTransaction(
        candidate_id=candidate.id,
        fund_code=candidate.fund_code,
        fund_name=candidate.fund_name,
        trade_date=candidate.trade_date,
        submitted_at=candidate.submitted_at,
        confirm_date=candidate.confirm_date,
        action=candidate.action,
        amount_cny=candidate.amount_cny,
        share=candidate.share,
        nav=candidate.nav,
        fee=fee,
        source_file=candidate.source_file,
        raw_text=candidate.raw_text,
    )
    session.add(tx)
    session.flush()
    candidate.status = CandidateStatus.confirmed
    candidate.confirmed_transaction_id = tx.id
    candidate.updated_at = datetime.utcnow()
    session.add(candidate)
    audit_log(session, "candidate.confirm", "candidate", str(candidate.id), f"transaction={tx.id}; {candidate.fund_code}")
    return True


def backfill_candidate_values(session: Session, candidate: FundTransactionCandidate) -> bool:
    if candidate.fund_code == "000000" or not candidate.trade_date:
        return False
    if not _candidate_needs_value_backfill(candidate):
        return False
    amount_cny, share, fee, confirm_date, trade_date = apply_trade_calculation(
        session,
        candidate.fund_code,
        candidate.action,
        candidate.amount_cny,
        candidate.share,
        candidate.nav,
        candidate.trade_date,
        submitted_at=candidate.submitted_at,
    )
    money = is_money_fund(session, candidate.fund_code)
    nav = 1.0 if money else (
        candidate.nav if candidate.nav is not None else nav_value_on_date(session, candidate.fund_code, trade_date)
    )
    changed = False
    for key, value in (
        ("amount_cny", amount_cny),
        ("share", share),
        ("fee", fee),
        ("confirm_date", confirm_date),
        ("nav", nav),
    ):
        if getattr(candidate, key) is None and value is not None:
            setattr(candidate, key, value)
            changed = True
    if candidate.nav is None and nav is not None and trade_date != candidate.trade_date:
        candidate.trade_date = trade_date
        changed = True
    if changed:
        candidate.updated_at = datetime.utcnow()
        session.add(candidate)
    return changed


def _candidate_needs_value_backfill(candidate: FundTransactionCandidate) -> bool:
    if candidate.action == TransactionAction.dividend:
        return candidate.amount_cny is None or candidate.confirm_date is None
    if candidate.action in {TransactionAction.buy, TransactionAction.sell, TransactionAction.dividend_reinvest}:
        return candidate.share is None or candidate.nav is None or candidate.confirm_date is None
    return candidate.confirm_date is None


def find_duplicate_transaction(
    session: Session,
    candidate: FundTransactionCandidate,
) -> FundTransaction | None:
    matches = session.exec(
        select(FundTransaction).where(
            FundTransaction.fund_code == candidate.fund_code,
            FundTransaction.trade_date == candidate.trade_date,
            FundTransaction.action == candidate.action,
            FundTransaction.submitted_at == candidate.submitted_at,
            FundTransaction.source_file == candidate.source_file,
        )
    ).all()
    for tx in matches:
        if tx.candidate_id == candidate.id:
            continue
        if _same_money_value(tx.amount_cny, candidate.amount_cny) and _same_money_value(tx.share, candidate.share):
            return tx
    return None


def _same_money_value(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return round(left, 2) == round(right, 2)


def candidate_quality(session: Session, candidate: FundTransactionCandidate) -> dict[str, Any]:
    issues: list[str] = []
    score = max(min(candidate.confidence or 0.0, 1.0), 0.0)
    if candidate.status != CandidateStatus.pending:
        issues.append(f"状态为 {candidate.status.value}")
    if candidate.fund_code == "000000" or not candidate.fund_code:
        issues.append("缺少基金代码")
        score -= 0.35
    if not candidate.fund_name:
        issues.append("缺少基金名称")
        score -= 0.08
    if not candidate.trade_date:
        issues.append("缺少交易日")
        score -= 0.25
    if not candidate.confirm_date:
        issues.append("缺少确认日")
        score -= 0.08
    if not _candidate_has_trade_values(session, candidate):
        issues.append("金额/份额/净值不完整")
        score -= 0.25
    if find_duplicate_transaction(session, candidate):
        issues.append("疑似重复流水")
        score -= 0.3
    if candidate.status == CandidateStatus.pending and candidate_duplicate_key(candidate):
        siblings = session.exec(
            select(FundTransactionCandidate).where(
                FundTransactionCandidate.status == CandidateStatus.pending,
                FundTransactionCandidate.source_file == candidate.source_file,
                FundTransactionCandidate.fund_code == candidate.fund_code,
                FundTransactionCandidate.trade_date == candidate.trade_date,
                FundTransactionCandidate.action == candidate.action,
                FundTransactionCandidate.submitted_at == candidate.submitted_at,
            )
        ).all()
        same_key_count = sum(1 for item in siblings if candidate_duplicate_key(item) == candidate_duplicate_key(candidate))
        if same_key_count > 1:
            issues.append("同文件重复候选")
            score -= 0.3
    score = round(max(score, 0.0), 2)
    auto_confirmable = (
        candidate.status == CandidateStatus.pending
        and score >= 0.8
        and not issues
    )
    if auto_confirmable:
        label = "高"
    elif score >= 0.6 and candidate.fund_code != "000000":
        label = "中"
    else:
        label = "低"
    return {
        "score": score,
        "label": label,
        "issues": issues,
        "auto_confirmable": auto_confirmable,
    }


def _candidate_has_trade_values(session: Session, candidate: FundTransactionCandidate) -> bool:
    if candidate.action == TransactionAction.dividend:
        return candidate.amount_cny is not None and candidate.amount_cny > 0
    if is_money_fund(session, candidate.fund_code):
        return (
            candidate.amount_cny is not None
            and candidate.amount_cny > 0
            and candidate.share is not None
            and candidate.share > 0
            and candidate.nav == 1.0
        )
    if candidate.action == TransactionAction.buy:
        return (
            candidate.amount_cny is not None
            and candidate.amount_cny > 0
            and candidate.share is not None
            and candidate.share > 0
            and candidate.nav is not None
            and candidate.nav > 0
        )
    if candidate.action == TransactionAction.sell:
        return (
            candidate.share is not None
            and candidate.share > 0
            and candidate.amount_cny is not None
            and candidate.amount_cny > 0
            and candidate.nav is not None
            and candidate.nav > 0
        )
    if candidate.action == TransactionAction.dividend_reinvest:
        return (
            candidate.amount_cny is not None
            and candidate.amount_cny > 0
            and candidate.share is not None
            and candidate.share > 0
            and candidate.nav is not None
            and candidate.nav > 0
        )
    return candidate.amount_cny is not None or candidate.share is not None


def candidate_duplicate_key(candidate: FundTransactionCandidate) -> tuple | None:
    if not candidate.source_file or candidate.fund_code == "000000":
        return None
    return (
        candidate.fund_code,
        candidate.trade_date,
        candidate.submitted_at.isoformat() if candidate.submitted_at else None,
        candidate.action.value,
        round(candidate.amount_cny, 2) if candidate.amount_cny is not None else None,
        round(candidate.share, 2) if candidate.share is not None else None,
        candidate.source_file,
    )


def duplicate_candidate_warning_ids(
    session: Session,
    candidates: list[FundTransactionCandidate],
) -> set[int]:
    warning_ids: set[int] = set()
    seen: dict[tuple, int] = {}
    for candidate in candidates:
        if candidate.status != CandidateStatus.pending:
            continue
        if find_duplicate_transaction(session, candidate) and candidate.id is not None:
            warning_ids.add(candidate.id)
        key = candidate_duplicate_key(candidate)
        if not key:
            continue
        first_id = seen.get(key)
        if first_id is None:
            if candidate.id is not None:
                seen[key] = candidate.id
            continue
        warning_ids.add(first_id)
        if candidate.id is not None:
            warning_ids.add(candidate.id)
    return warning_ids


@app.post("/candidates/{candidate_id}/suggest-code")
def candidate_suggest_code(
    candidate_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    candidate = session.get(FundTransactionCandidate, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404)
    if not candidate.fund_name or (candidate.fund_code and candidate.fund_code != "000000"):
        return redirect("/candidates")
    result = search_fund_by_name(candidate.fund_name)
    if result:
        code = result["fund_code"]
        candidate.fund_code = code
        candidate.confidence = max(candidate.confidence, 0.6)
        candidate.updated_at = datetime.utcnow()
        session.add(candidate)
        session.commit()
        message = f"名称匹配 {candidate.fund_name} → {code}"
    else:
        message = "无法匹配基金代码，请手动填写"
    return redirect(f"/candidates?message={message}")


@app.post("/candidates/fix-unmatched")
def candidates_fix_unmatched(
    fund_name: str = Form(...),
    fund_code: str = Form(...),
    return_to: str = Form("/candidates"),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    code = fund_code.strip().zfill(6)
    if not code.isdigit() or len(code) != 6:
        return candidates_redirect_with_message(return_to, "无效的基金代码")
    candidates = session.exec(
        select(FundTransactionCandidate).where(
            FundTransactionCandidate.fund_name == fund_name,
            FundTransactionCandidate.fund_code == "000000",
            FundTransactionCandidate.status == CandidateStatus.pending,
        )
    ).all()
    if candidates:
        _apply_unmatched_code_fix(session, candidates, fund_name, code)
        fixed = len(candidates)
        messages = [f"已将 {fixed} 条「{fund_name}」的代码修正为 {code}"]
        try:
            synced = fetch_fund_rule_from_akshare(code)
            rule = session.get(FundRule, code) or FundRule(fund_code=code)
            rule.fund_name = synced.fund_name or fund_name
            rule.fund_type = synced.fund_type or rule.fund_type
            if synced.buy_confirm_days is not None:
                rule.buy_confirm_days = synced.buy_confirm_days
            if synced.sell_confirm_days is not None:
                rule.sell_confirm_days = synced.sell_confirm_days
            if synced.buy_fee_rate is not None:
                rule.buy_fee_rate = synced.buy_fee_rate
            rule.sync_source = synced.source
            rule.synced_at = sync_timestamp()
            rule.updated_at = datetime.utcnow()
            session.add(rule)
            session.execute(
                text("UPDATE fundtransaction SET fund_code = :code WHERE fund_code = '000000' AND fund_name = :name"),
                {"code": code, "name": fund_name},
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            _apply_unmatched_code_fix(session, candidates, fund_name, code)
            messages.append(f"规则同步失败：{exc}")
        try:
            _, error = sync_nav_for_fund(session, code)
            if error:
                messages.append(f"净值同步失败：{error}")
        except Exception as exc:
            messages.append(f"净值同步失败：{exc}")
        return candidates_redirect_with_message(return_to, "；".join(messages))
    return candidates_redirect_with_message(return_to, "未找到需要修正的候选")


def _apply_unmatched_code_fix(
    session: Session,
    candidates: list[FundTransactionCandidate],
    fund_name: str,
    code: str,
) -> None:
    for candidate in candidates:
        candidate.fund_code = code
        candidate.confidence = 0.8
        candidate.updated_at = datetime.utcnow()
        session.add(candidate)
    session.execute(
        text("UPDATE fundtransaction SET fund_code = :code WHERE fund_code = '000000' AND fund_name = :name"),
        {"code": code, "name": fund_name},
    )
    session.commit()


@app.get("/transactions", response_class=HTMLResponse)
def transactions_page(
    request: Request,
    message: str = "",
    fund_code: str = "",
    action: str = "",
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    query = select(FundTransaction)
    code = fund_code.strip().zfill(6) if fund_code.strip() else ""
    if code:
        query = query.where(FundTransaction.fund_code == code)
    if action in {item.value for item in TransactionAction}:
        query = query.where(FundTransaction.action == TransactionAction(action))
    if date_from:
        query = query.where(FundTransaction.trade_date >= date_from)
    if date_to:
        query = query.where(FundTransaction.trade_date <= date_to)
    transactions = session.exec(query.order_by(desc(FundTransaction.trade_date), desc(FundTransaction.id))).all()
    return_to = transactions_url(code, action, date_from, date_to)
    return templates.TemplateResponse(
        "transactions.html",
        {
            "request": request,
            "transactions": transactions,
            "actions": list(TransactionAction),
            "message": message,
            "filters": {
                "fund_code": code,
                "action": action,
                "date_from": date_from,
                "date_to": date_to,
            },
            "return_to": return_to,
        },
    )


@app.post("/transactions")
def transaction_create(
    fund_code: str = Form(...),
    fund_name: str = Form(""),
    trade_date: date = Form(...),
    submitted_at: Optional[time] = Form(None),
    confirm_date: Optional[date] = Form(None),
    action: TransactionAction = Form(...),
    amount_cny: Optional[float] = Form(None),
    share: Optional[float] = Form(None),
    nav: Optional[float] = Form(None),
    fee: Optional[float] = Form(None),
    note: str = Form(""),
    return_to: str = Form("/transactions"),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    code = fund_code.strip().zfill(6)
    if not code.isdigit() or len(code) != 6:
        return transactions_redirect_with_message(return_to, "无效的基金代码")
    rule = get_fund_rule(session, code)
    amount_cny, share, fee, confirm_date, trade_date, nav = calculate_manual_transaction_values(
        session,
        code,
        action,
        amount_cny,
        share,
        nav,
        fee,
        trade_date,
        submitted_at,
        confirm_date,
    )
    tx = FundTransaction(
        fund_code=code,
        fund_name=fund_name.strip() or rule.fund_name,
        trade_date=trade_date,
        submitted_at=submitted_at,
        confirm_date=confirm_date,
        action=action,
        amount_cny=amount_cny,
        share=share,
        nav=nav,
        fee=fee,
        source_file="manual",
        raw_text=note.strip(),
    )
    session.add(tx)
    session.flush()
    audit_log(session, "transaction.create", "transaction", str(tx.id), f"{code} {action.value} {amount_cny}")
    session.commit()
    return transactions_redirect_with_message(return_to, "手动流水已新增")


@app.post("/transactions/{transaction_id}/update")
def transaction_update(
    transaction_id: int,
    fund_code: str = Form(...),
    fund_name: str = Form(""),
    trade_date: date = Form(...),
    submitted_at: Optional[time] = Form(None),
    confirm_date: Optional[date] = Form(None),
    action: TransactionAction = Form(...),
    amount_cny: Optional[float] = Form(None),
    share: Optional[float] = Form(None),
    nav: Optional[float] = Form(None),
    fee: Optional[float] = Form(None),
    note: str = Form(""),
    return_to: str = Form("/transactions"),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    tx = session.get(FundTransaction, transaction_id)
    if not tx:
        raise HTTPException(status_code=404)
    code = fund_code.strip().zfill(6)
    if not code.isdigit() or len(code) != 6:
        return transactions_redirect_with_message(return_to, "无效的基金代码")
    rule = get_fund_rule(session, code)
    amount_cny, share, fee, confirm_date, trade_date, nav = calculate_manual_transaction_values(
        session,
        code,
        action,
        amount_cny,
        share,
        nav,
        fee,
        trade_date,
        submitted_at,
        confirm_date,
    )
    tx.fund_code = code
    tx.fund_name = fund_name.strip() or rule.fund_name
    tx.trade_date = trade_date
    tx.submitted_at = submitted_at
    tx.confirm_date = confirm_date
    tx.action = action
    tx.amount_cny = amount_cny
    tx.share = share
    tx.nav = nav
    tx.fee = fee
    tx.raw_text = note.strip()
    session.add(tx)
    audit_log(session, "transaction.update", "transaction", str(tx.id), f"{code} {action.value} {amount_cny}")
    session.commit()
    return transactions_redirect_with_message(return_to, "流水已更新")


@app.post("/transactions/{transaction_id}/delete")
def transaction_delete(
    transaction_id: int,
    return_to: str = Form("/transactions"),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    tx = session.get(FundTransaction, transaction_id)
    if not tx:
        raise HTTPException(status_code=404)
    if tx.candidate_id:
        candidate = session.get(FundTransactionCandidate, tx.candidate_id)
        if candidate:
            candidate.status = CandidateStatus.pending
            candidate.confirmed_transaction_id = None
            candidate.updated_at = datetime.utcnow()
            session.add(candidate)
    tx_id = tx.id
    detail = f"{tx.fund_code} {tx.action.value} {tx.amount_cny}"
    session.delete(tx)
    audit_log(session, "transaction.delete", "transaction", str(tx_id), detail)
    session.commit()
    return transactions_redirect_with_message(return_to, "流水已删除")


@app.get("/holdings", response_class=HTMLResponse)
def holdings_page(
    request: Request,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    positions = calculate_position_summaries(session)
    return templates.TemplateResponse(
        "holdings.html",
        {
            "request": request,
            "holdings": [item for item in positions if not item.is_closed],
            "closed_positions": [item for item in positions if item.is_closed],
            "xalpha_rows": xalpha_rows(session),
        },
    )


@app.get("/holdings/{fund_code}", response_class=HTMLResponse)
def holding_detail_page(
    fund_code: str,
    request: Request,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    fund_code = fund_code.zfill(6)
    positions = calculate_position_summaries(session)
    position = next((item for item in positions if item.fund_code == fund_code), None)
    if not position:
        raise HTTPException(status_code=404, detail="position not found")
    transactions = session.exec(
        select(FundTransaction)
        .where(FundTransaction.fund_code == fund_code)
        .order_by(FundTransaction.trade_date, FundTransaction.id)
    ).all()
    charts = build_performance_charts(session, include_closed=True, fund_code=fund_code)
    return templates.TemplateResponse(
        "holding_detail.html",
        {
            "request": request,
            "position": position,
            "transactions": transactions,
            "chart": charts[0] if charts else None,
            "format_return": format_return,
        },
    )


@app.get("/performance", response_class=HTMLResponse)
def performance_page(
    request: Request,
    message: str = "",
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    charts = build_performance_charts(session)
    latest_benchmark = session.exec(
        select(BenchmarkNav)
        .where(BenchmarkNav.benchmark_code == "000300")
        .order_by(desc(BenchmarkNav.nav_date))
    ).first()
    jobs = session.exec(
        select(BackgroundJob)
        .where(BackgroundJob.job_type == "sync_benchmark")
        .order_by(desc(BackgroundJob.created_at))
        .limit(5)
    ).all()
    return templates.TemplateResponse(
        "performance.html",
        {
            "request": request,
            "message": message,
            "charts": charts,
            "latest_benchmark": latest_benchmark,
            "jobs": jobs,
            "format_return": format_return,
        },
    )


@app.post("/performance/benchmark/sync")
def performance_benchmark_sync(
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    job = create_and_enqueue(session, "sync_benchmark", {})
    return redirect(f"/performance?message=沪深300同步已加入后台任务 #{job.id}")


@app.get("/nav", response_class=HTMLResponse)
def nav_page(
    request: Request,
    message: str = "",
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    funds = sorted(
        {tx.fund_code for tx in session.exec(select(FundTransaction)).all()}
        | {c.fund_code for c in session.exec(select(FundTransactionCandidate)).all()}
    )
    latest = {
        code: session.exec(
            select(FundNav).where(FundNav.fund_code == code).order_by(desc(FundNav.nav_date))
        ).first()
        for code in funds
    }
    jobs = session.exec(
        select(BackgroundJob)
        .where(BackgroundJob.job_type.in_(["sync_nav", "daily_market_sync"]))
        .order_by(desc(BackgroundJob.created_at))
        .limit(5)
    ).all()
    return templates.TemplateResponse(
        "nav.html", {"request": request, "funds": funds, "latest": latest, "message": message, "jobs": jobs}
    )


@app.post("/nav/sync")
def nav_sync(
    fund_code: str = Form(...),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    job = create_and_enqueue(session, "sync_nav", {"fund_code": fund_code.zfill(6)})
    return redirect(f"/nav?message=净值同步已加入后台任务 #{job.id}")


@app.post("/nav/sync-current")
def nav_sync_current(
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    job = create_and_enqueue(session, "daily_market_sync", {"manual": True})
    return redirect(f"/nav?message=当前持仓和曲线同步已加入后台任务 #{job.id}")


@app.get("/backup", response_class=HTMLResponse)
def backup_page(
    request: Request,
    message: str = "",
    _: str = Depends(require_user),
):
    backups = sorted(backup_dir().glob("fund-ledger-backup-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:20]
    return templates.TemplateResponse(
        "backup.html",
        {
            "request": request,
            "message": message,
            "backups": [{"name": item.name, "size": item.stat().st_size, "mtime": datetime.utcfromtimestamp(item.stat().st_mtime)} for item in backups],
        },
    )


@app.post("/backup/create")
def backup_create(
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    path = write_backup_file(session)
    audit_log(session, "backup.manual_file", "backup", path.name, str(path))
    session.commit()
    return redirect(f"/backup?message=备份文件已生成：{path.name}")


@app.post("/backup/preview", response_class=HTMLResponse)
async def backup_preview(
    request: Request,
    file: UploadFile = File(...),
    _: str = Depends(require_user),
):
    try:
        content = (await file.read()).decode("utf-8")
        payload = json.loads(content)
        counts = backup_counts(payload)
    except Exception as exc:
        return templates.TemplateResponse(
            "backup.html",
            {"request": request, "message": f"备份解析失败：{exc}"},
            status_code=400,
        )
    return templates.TemplateResponse(
        "backup.html",
        {
            "request": request,
            "message": "备份预览已生成",
            "backup_json": json.dumps(payload, ensure_ascii=False),
            "counts": counts,
            "version": payload.get("version"),
            "exported_at": payload.get("exported_at"),
        },
    )


@app.post("/backup/restore")
def backup_restore(
    request: Request,
    backup_json: str = Form(...),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    try:
        payload = json.loads(backup_json)
        counts = restore_backup_payload(session, payload)
    except Exception as exc:
        return templates.TemplateResponse(
            "backup.html",
            {"request": request, "message": f"恢复失败：{exc}"},
            status_code=400,
        )
    audit_log(session, "backup.restore", "backup", detail=json.dumps(counts, ensure_ascii=False))
    session.commit()
    message = "恢复完成：" + "，".join(f"{key} {value}" for key, value in counts.items())
    return templates.TemplateResponse("backup.html", {"request": request, "message": message})


@app.get("/backup/export")
def backup_export(
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    payload = create_backup_payload(session)
    response = JSONResponse(payload)
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    response.headers["Content-Disposition"] = f"attachment; filename=fund-ledger-backup-{stamp}.json"
    return response


def create_backup_payload(session: Session) -> dict[str, Any]:
    return {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "version": 1,
        "candidates": [
            serialize_model(item)
            for item in session.exec(select(FundTransactionCandidate).order_by(FundTransactionCandidate.id)).all()
        ],
        "imports": [
            serialize_model(item)
            for item in session.exec(select(ImportDocument).order_by(ImportDocument.id)).all()
        ],
        "settings": [
            {**serialize_model(item), "value": "***" if item.is_secret and item.value else item.value}
            for item in session.exec(select(AppSetting).order_by(AppSetting.key)).all()
        ],
        "fund_rules": [
            serialize_model(item)
            for item in session.exec(select(FundRule).order_by(FundRule.fund_code)).all()
        ],
        "fund_fee_tiers": [
            serialize_model(item)
            for item in session.exec(select(FundFeeTier).order_by(FundFeeTier.fund_code)).all()
        ],
        "transactions": [
            serialize_model(item)
            for item in session.exec(select(FundTransaction).order_by(FundTransaction.id)).all()
        ],
        "nav": [
            serialize_model(item)
            for item in session.exec(select(FundNav).order_by(FundNav.fund_code, FundNav.nav_date)).all()
        ],
        "benchmark_nav": [
            serialize_model(item)
            for item in session.exec(select(BenchmarkNav).order_by(BenchmarkNav.benchmark_code, BenchmarkNav.nav_date)).all()
        ],
        "aliases": [
            serialize_model(item)
            for item in session.exec(select(FundAlias).order_by(FundAlias.id)).all()
        ],
        "audits": [
            serialize_model(item)
            for item in session.exec(select(OperationAudit).order_by(OperationAudit.id)).all()
        ],
    }

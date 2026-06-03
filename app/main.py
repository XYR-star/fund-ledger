import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, desc, select
from starlette.status import HTTP_303_SEE_OTHER

from .app_settings import configured, masked, runtime_settings, save_settings
from .auth import add_session_middleware, current_user, login_user, logout_user, verify_login
from .config import ensure_data_dirs, settings
from .db import engine, get_session, init_db
from .extractors import extract_candidates, hash_content, hash_file
from .fund_rule_sync import fetch_fund_rule_from_akshare, sync_timestamp
from .jobs import create_and_enqueue, recover_interrupted_jobs, register_job
from .llm import is_deepseek_configured, parse_with_deepseek
from .models import (
    BackgroundJob,
    BenchmarkNav,
    CandidateStatus,
    AppSetting,
    FundFeeTier,
    FundNav,
    FundRule,
    FundTransaction,
    FundTransactionCandidate,
    ImportDocument,
    ImportStatus,
    TransactionAction,
)
from .nav import sync_nav_for_fund
from .ocr import recognize_file
from .performance import build_performance_charts, format_return, sync_hs300
from .portfolio import calculate_holdings, xalpha_rows
from .templates import templates


app = FastAPI(title="Fund Ledger")
add_session_middleware(app)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    register_background_jobs()
    recover_interrupted_jobs()


def register_background_jobs() -> None:
    register_job("auto_import", process_auto_import_job)
    register_job("ocr_import", process_ocr_job)
    register_job("parse_import", process_parse_job)
    register_job("sync_nav", process_nav_job)
    register_job("sync_benchmark", process_benchmark_job)
    register_job("sync_fund_rule", process_fund_rule_sync_job)


def require_user(request: Request) -> str:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    return user


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=HTTP_303_SEE_OTHER)


def serialize_model(model):
    data = model.model_dump()
    for key, value in data.items():
        if isinstance(value, (date, datetime)):
            data[key] = value.isoformat()
        elif hasattr(value, "value"):
            data[key] = value.value
    return data


def create_candidates_from_text(
    session: Session,
    raw_text: str,
    source_file: str | None = None,
    source_hash: str | None = None,
) -> int:
    extracted = extract_candidates(raw_text)
    if not extracted:
        return create_inferred_candidates_from_minimal_text(
            session,
            raw_text,
            source_file=source_file,
            source_hash=source_hash,
        )
    for item in extracted:
        session.add(
            FundTransactionCandidate(
                fund_code=item.fund_code,
                fund_name=item.fund_name,
                trade_date=item.trade_date,
                confirm_date=item.confirm_date,
                action=item.action,
                amount_cny=item.amount_cny,
                share=item.share,
                nav=item.nav,
                fee=item.fee,
                source_file=source_file,
                source_hash=source_hash,
                raw_text=item.raw_text,
                confidence=item.confidence,
            )
        )
    return len(extracted)


def create_inferred_candidates_from_minimal_text(
    session: Session,
    raw_text: str,
    source_file: str | None = None,
    source_hash: str | None = None,
) -> int:
    created = 0
    known_names = known_fund_names(session)
    for line in raw_text.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        trade_day, submitted_at = extract_trade_datetime(text)
        amount = extract_amount(text)
        if not trade_day or amount is None:
            continue
        action = infer_action(text)
        status = infer_candidate_status(text)
        fund_code = extract_fund_code(text)
        fund_name = extract_fund_name(text, fund_code, known_names)
        if not fund_code and fund_name in known_names:
            fund_code = known_names[fund_name]
        fund_code = (fund_code or "000000").zfill(6)
        rule = get_fund_rule(session, fund_code)
        nav_item = find_effective_nav(session, fund_code, trade_day, submitted_at, rule)
        confirm_date = (
            find_nth_nav_date(
                session,
                fund_code,
                nav_item.nav_date,
                rule.buy_confirm_days if action == TransactionAction.buy else rule.sell_confirm_days,
            )
            if nav_item
            else None
        )
        nav_value = nav_item.unit_nav if nav_item else None
        fee = 0.0 if action == TransactionAction.buy else None
        share = None
        if action == TransactionAction.buy and nav_value:
            fee = round(amount * rule.buy_fee_rate, 2)
            share = round((amount - fee) / nav_value, 4)
        if action == TransactionAction.sell and nav_value:
            fee = infer_redemption_fee(session, fund_code, amount, nav_value, nav_item.nav_date)
        session.add(
            FundTransactionCandidate(
                status=status,
                fund_code=fund_code,
                fund_name=fund_name,
                trade_date=nav_item.nav_date if nav_item else trade_day,
                confirm_date=confirm_date,
                action=action,
                amount_cny=amount if action != TransactionAction.sell else None,
                share=share if action != TransactionAction.sell else amount,
                nav=nav_value,
                fee=fee,
                source_file=source_file,
                source_hash=source_hash,
                raw_text=text,
                confidence=0.55 if fund_code == "000000" else 0.75,
            )
        )
        created += 1
    return created


def known_fund_names(session: Session) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in session.exec(select(FundTransactionCandidate)).all():
        if item.fund_name and item.fund_code != "000000":
            names[item.fund_name] = item.fund_code
    for item in session.exec(select(FundTransaction)).all():
        if item.fund_name and item.fund_code != "000000":
            names[item.fund_name] = item.fund_code
    return names


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
    for name in sorted(known_names, key=len, reverse=True):
        if name and name in text:
            return name
    cleaned = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", " ", text)
    cleaned = re.sub(r"\d{1,2}:\d{2}", " ", cleaned)
    if fund_code:
        cleaned = cleaned.replace(fund_code, " ")
    cleaned = re.sub(r"[买入申购卖出赎回成功失败撤销取消已确认交易金额人民币元：:,.，。\d\s-]+", " ", cleaned)
    parts = [part for part in re.split(r"\s+", cleaned.strip()) if part]
    return parts[0] if parts else ""


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
    if any(word in text for word in ("撤销", "取消", "失败", "未成功")):
        return CandidateStatus.ignored
    return CandidateStatus.pending


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
    transactions = session.exec(
        select(FundTransaction)
        .where(FundTransaction.fund_code == fund_code)
        .order_by(FundTransaction.trade_date, FundTransaction.id)
    ).all()
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


def create_candidates_from_rows(
    session: Session,
    rows: list[dict[str, Any]],
    source_file: str | None = None,
    source_hash: str | None = None,
) -> int:
    created = 0
    for row in rows:
        trade_date = parse_date_value(row.get("trade_date"))
        fund_code = str(row.get("fund_code") or "").strip().zfill(6)
        if not trade_date or not fund_code.isdigit() or len(fund_code) != 6:
            continue
        try:
            action = TransactionAction(str(row.get("action") or TransactionAction.buy.value))
        except ValueError:
            action = TransactionAction.buy
        candidate = FundTransactionCandidate(
            fund_code=fund_code,
            fund_name=str(row.get("fund_name") or ""),
            trade_date=trade_date,
            confirm_date=parse_date_value(row.get("confirm_date")),
            action=action,
            amount_cny=parse_float_value(row.get("amount_cny")),
            share=parse_float_value(row.get("share")),
            nav=parse_float_value(row.get("nav")),
            fee=parse_float_value(row.get("fee")),
            source_file=source_file,
            source_hash=source_hash,
            raw_text=str(row),
            confidence=0.85,
        )
        session.add(candidate)
        created += 1
    return created


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


def parse_int_value(value: Any) -> int | None:
    if value in (None, "", "-", "null"):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


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
                    document.ocr_text = llm_result.raw_response
                    parsed_count = create_candidates_from_rows(
                        session,
                        llm_result.parsed_json,
                        source_file=document.source_file,
                        source_hash=document.source_hash,
                    )
                    messages.append("DeepSeek 解析")

        if parsed_count == 0:
            parsed_count = create_candidates_from_text(
                session,
                text,
                source_file=document.source_file,
                source_hash=document.source_hash,
            )
            messages.append("规则解析")

        document.status = ImportStatus.parse_done
        document.updated_at = datetime.utcnow()
        session.add(document)
        session.commit()

        fund_codes = fund_codes_for_source(session, document.source_hash)
        sync_errors = sync_related_market_data(session, fund_codes)
        if sync_errors:
            document.error_message = "\n".join(sync_errors)
            document.updated_at = datetime.utcnow()
            session.add(document)
            session.commit()
        messages.append(f"候选 {parsed_count} 条")
        if fund_codes:
            messages.append(f"基金 {', '.join(sorted(fund_codes))}")
        return "；".join(messages)


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
        config = runtime_settings(session)
        if use_llm and is_deepseek_configured(config):
            llm_result = parse_with_deepseek(text, config)
            if llm_result and llm_result.parsed_json:
                document.ocr_text = llm_result.raw_response
                parsed_count = create_candidates_from_rows(
                    session,
                    llm_result.parsed_json,
                    source_file=document.source_file,
                    source_hash=document.source_hash,
                )
        if parsed_count == 0:
            parsed_count = create_candidates_from_text(
                session,
                text,
                source_file=document.source_file,
                source_hash=document.source_hash,
            )
        document.status = ImportStatus.parse_done
        document.error_message = ""
        document.updated_at = datetime.utcnow()
        session.add(document)
        session.commit()
        return f"已生成 {parsed_count} 条候选交易"


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


def sync_related_market_data(session: Session, fund_codes: set[str]) -> list[str]:
    errors = []
    for code in sorted(fund_codes):
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
        except Exception as exc:
            errors.append(f"{code} 规则同步失败：{exc}")

        inserted, error = sync_nav_for_fund(session, code)
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
        return f"{code} 规则同步完成"


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
        session.add(FundTransactionCandidate(**_model_data(item, {"trade_date", "confirm_date", "created_at", "updated_at"})))
        counts["candidates"] += 1

    for item in payload.get("transactions") or []:
        existing = session.get(FundTransaction, item.get("id"))
        if existing:
            continue
        session.add(FundTransaction(**_model_data(item, {"trade_date", "confirm_date", "created_at"})))
        counts["transactions"] += 1

    for item in payload.get("fund_fee_tiers") or []:
        existing = session.get(FundFeeTier, item.get("id"))
        if existing:
            continue
        session.add(FundFeeTier(**_model_data(item, {"updated_at"})))
        counts["fund_fee_tiers"] += 1

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
        if key.endswith("_date") or key == "nav_date":
            data[key] = parse_date_value(data[key])
        else:
            data[key] = _parse_datetime_value(data[key])
    return data


def _apply_fields(model: Any, item: dict[str, Any], skip: set[str]) -> None:
    for key, value in item.items():
        if key in skip or not hasattr(model, key):
            continue
        if key.endswith("_date") or key == "nav_date":
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
    return templates.TemplateResponse(
        "fund_rules.html",
        {"request": request, "rules": rules, "tiers_by_code": tiers_by_code, "message": message, "jobs": jobs},
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
            document = await create_import_document_from_upload(session, upload, raw_text)
            documents.append(document)
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
    return templates.TemplateResponse(
        "imports.html",
        {
            "request": request,
            "documents": documents,
            "show": show,
            "message": message,
            "llm_configured": is_deepseek_configured(config),
        },
    )


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
    return templates.TemplateResponse(
        "import_detail.html",
        {
            "request": request,
            "document": document,
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
    if document.source_file:
        path = Path(document.source_file)
        if path.exists() and path.is_file():
            path.unlink()
    document.status = ImportStatus.deleted
    document.source_file = None
    document.updated_at = datetime.utcnow()
    session.add(document)
    session.commit()
    return redirect("/imports")


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
    document.status = ImportStatus.ocr_running
    document.error_message = ""
    document.updated_at = datetime.utcnow()
    session.add(document)
    session.commit()
    job = create_and_enqueue(session, "ocr_import", {"document_id": document_id})
    return redirect(f"/imports/{document_id}?message=OCR 已加入后台任务 #{job.id}")


@app.post("/imports/{document_id}/text")
def import_update_text(
    document_id: int,
    raw_text: str = Form(""),
    ocr_text: str = Form(""),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    document = session.get(ImportDocument, document_id)
    if not document:
        raise HTTPException(status_code=404)
    document.raw_text = raw_text
    document.ocr_text = ocr_text
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
    document.error_message = ""
    session.add(document)
    session.commit()
    job = create_and_enqueue(session, "parse_import", {"document_id": document_id, "use_llm": use_llm})
    return redirect(f"/imports/{document_id}?message=解析已加入后台任务 #{job.id}")


@app.get("/candidates", response_class=HTMLResponse)
def candidates_page(
    request: Request,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    candidates = session.exec(
        select(FundTransactionCandidate).order_by(
            FundTransactionCandidate.status, desc(FundTransactionCandidate.created_at)
        )
    ).all()
    return templates.TemplateResponse(
        "candidates.html",
        {"request": request, "candidates": candidates, "actions": list(TransactionAction)},
    )


@app.post("/candidates/{candidate_id}/update")
def candidate_update(
    candidate_id: int,
    fund_code: str = Form(...),
    fund_name: str = Form(""),
    trade_date: date = Form(...),
    confirm_date: Optional[date] = Form(None),
    action: TransactionAction = Form(...),
    amount_cny: Optional[float] = Form(None),
    share: Optional[float] = Form(None),
    nav: Optional[float] = Form(None),
    fee: Optional[float] = Form(None),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    candidate = session.get(FundTransactionCandidate, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404)
    candidate.fund_code = fund_code.zfill(6)
    candidate.fund_name = fund_name
    candidate.trade_date = trade_date
    candidate.confirm_date = confirm_date
    candidate.action = action
    candidate.amount_cny = amount_cny
    candidate.share = share
    candidate.nav = nav
    candidate.fee = fee
    candidate.updated_at = datetime.utcnow()
    session.add(candidate)
    session.commit()
    return redirect("/candidates")


@app.post("/candidates/{candidate_id}/confirm")
def candidate_confirm(
    candidate_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    candidate = session.get(FundTransactionCandidate, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404)
    if candidate.status == CandidateStatus.confirmed:
        return redirect("/candidates")
    tx = FundTransaction(
        candidate_id=candidate.id,
        fund_code=candidate.fund_code,
        fund_name=candidate.fund_name,
        trade_date=candidate.trade_date,
        confirm_date=candidate.confirm_date,
        action=candidate.action,
        amount_cny=candidate.amount_cny,
        share=candidate.share,
        nav=candidate.nav,
        fee=candidate.fee,
        source_file=candidate.source_file,
        raw_text=candidate.raw_text,
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)
    candidate.status = CandidateStatus.confirmed
    candidate.confirmed_transaction_id = tx.id
    candidate.updated_at = datetime.utcnow()
    session.add(candidate)
    session.commit()
    return redirect("/candidates")


@app.post("/candidates/{candidate_id}/ignore")
def candidate_ignore(
    candidate_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    candidate = session.get(FundTransactionCandidate, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404)
    if candidate.status != CandidateStatus.confirmed:
        candidate.status = CandidateStatus.ignored
        candidate.updated_at = datetime.utcnow()
        session.add(candidate)
        session.commit()
    return redirect("/candidates")


@app.get("/transactions", response_class=HTMLResponse)
def transactions_page(
    request: Request,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    transactions = session.exec(
        select(FundTransaction).order_by(desc(FundTransaction.trade_date))
    ).all()
    return templates.TemplateResponse(
        "transactions.html", {"request": request, "transactions": transactions}
    )


@app.get("/holdings", response_class=HTMLResponse)
def holdings_page(
    request: Request,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    return templates.TemplateResponse(
        "holdings.html",
        {"request": request, "holdings": calculate_holdings(session), "xalpha_rows": xalpha_rows(session)},
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
        .where(BackgroundJob.job_type == "sync_nav")
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


@app.get("/backup", response_class=HTMLResponse)
def backup_page(
    request: Request,
    message: str = "",
    _: str = Depends(require_user),
):
    return templates.TemplateResponse("backup.html", {"request": request, "message": message})


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
    message = "恢复完成：" + "，".join(f"{key} {value}" for key, value in counts.items())
    return templates.TemplateResponse("backup.html", {"request": request, "message": message})


@app.get("/backup/export")
def backup_export(
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    payload = {
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
    }
    response = JSONResponse(payload)
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    response.headers["Content-Disposition"] = f"attachment; filename=fund-ledger-backup-{stamp}.json"
    return response

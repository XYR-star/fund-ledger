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
from .db import get_session, init_db
from .extractors import extract_candidates, hash_content, hash_file
from .llm import is_deepseek_configured, parse_with_deepseek
from .models import (
    CandidateStatus,
    AppSetting,
    FundNav,
    FundTransaction,
    FundTransactionCandidate,
    ImportDocument,
    ImportStatus,
    TransactionAction,
)
from .nav import sync_nav_for_fund
from .ocr import recognize_file
from .portfolio import calculate_holdings, xalpha_rows
from .templates import templates


app = FastAPI(title="Fund Ledger")
add_session_middleware(app)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.on_event("startup")
def on_startup() -> None:
    init_db()


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
        nav_item = find_effective_nav(session, fund_code, trade_day, submitted_at)
        confirm_date = find_next_nav_date(session, fund_code, nav_item.nav_date) if nav_item else None
        nav_value = nav_item.unit_nav if nav_item else None
        fee = 0.0 if action == TransactionAction.buy else None
        share = None
        if action == TransactionAction.buy and nav_value:
            share = round((amount - (fee or 0)) / nav_value, 4)
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
) -> FundNav | None:
    if fund_code == "000000":
        return None
    target = trade_day + timedelta(days=1) if submitted_at and submitted_at >= time(15, 0) else trade_day
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


def find_next_nav_date(session: Session, fund_code: str, nav_date: date) -> date | None:
    item = session.exec(
        select(FundNav)
        .where(FundNav.fund_code == fund_code, FundNav.nav_date > nav_date)
        .order_by(FundNav.nav_date)
    ).first()
    return item.nav_date if item else None


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


@app.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, _: str = Depends(require_user)):
    return templates.TemplateResponse("upload.html", {"request": request})


@app.post("/upload")
async def upload_submit(
    request: Request,
    raw_text: str = Form(""),
    file: Optional[UploadFile] = File(None),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    ensure_data_dirs()
    source_file = None
    source_hash = hash_content(raw_text.encode())
    document = ImportDocument(
        raw_text=raw_text,
        source_hash=source_hash,
        status=ImportStatus.uploaded,
    )
    if file and file.filename:
        content = await file.read()
        safe_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{Path(file.filename).name}"
        target = settings.uploads_dir / safe_name
        target.write_bytes(content)
        source_file = str(target)
        source_hash = hash_file(target)
        document.file_name = file.filename
        document.source_file = source_file
        document.source_hash = source_hash
        document.content_type = file.content_type

    session.add(document)
    session.flush()
    create_candidates_from_text(session, raw_text, source_file=source_file, source_hash=source_hash)
    session.commit()
    return redirect(f"/imports/{document.id}")


@app.get("/imports", response_class=HTMLResponse)
def imports_page(
    request: Request,
    show: str = "",
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
    return templates.TemplateResponse(
        "import_detail.html",
        {
            "request": request,
            "document": document,
            "message": message,
            "llm_configured": is_deepseek_configured(config),
            "ocr_backend": config.get("OCR_BACKEND", "rapidocr"),
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
        return redirect(f"/imports/{document_id}?message=OCR 失败：{exc}")
    document.ocr_text = result.text
    document.status = ImportStatus.ocr_done
    document.error_message = ""
    document.updated_at = datetime.utcnow()
    session.add(document)
    session.commit()
    return redirect(f"/imports/{document_id}?message=OCR 完成")


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
    document.updated_at = datetime.utcnow()
    session.add(document)
    session.commit()
    return redirect(f"/imports/{document_id}?message=已生成 {parsed_count} 条候选交易")


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
    return templates.TemplateResponse(
        "nav.html", {"request": request, "funds": funds, "latest": latest, "message": message}
    )


@app.post("/nav/sync")
def nav_sync(
    fund_code: str = Form(...),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    inserted, error = sync_nav_for_fund(session, fund_code.zfill(6))
    if error:
        return redirect(f"/nav?message=同步失败：{error}")
    return redirect(f"/nav?message=同步完成，新增 {inserted} 条")


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
        "transactions": [
            serialize_model(item)
            for item in session.exec(select(FundTransaction).order_by(FundTransaction.id)).all()
        ],
        "nav": [
            serialize_model(item)
            for item in session.exec(select(FundNav).order_by(FundNav.fund_code, FundNav.nav_date)).all()
        ],
    }
    response = JSONResponse(payload)
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    response.headers["Content-Disposition"] = f"attachment; filename=fund-ledger-backup-{stamp}.json"
    return response

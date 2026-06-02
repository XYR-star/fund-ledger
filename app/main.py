from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, desc, select
from starlette.status import HTTP_303_SEE_OTHER

from .auth import add_session_middleware, current_user, login_user, logout_user, verify_login
from .config import ensure_data_dirs, settings
from .db import get_session, init_db
from .extractors import extract_candidates, hash_content, hash_file
from .llm import is_deepseek_configured, parse_with_deepseek
from .models import (
    CandidateStatus,
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
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    documents = session.exec(select(ImportDocument).order_by(desc(ImportDocument.created_at))).all()
    return templates.TemplateResponse(
        "imports.html",
        {"request": request, "documents": documents, "llm_configured": is_deepseek_configured()},
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
    return templates.TemplateResponse(
        "import_detail.html",
        {
            "request": request,
            "document": document,
            "message": message,
            "llm_configured": is_deepseek_configured(),
        },
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
    document.status = ImportStatus.ocr_running
    document.updated_at = datetime.utcnow()
    session.add(document)
    session.commit()
    try:
        result = recognize_file(document.source_file)
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
    if use_llm and is_deepseek_configured():
        llm_result = parse_with_deepseek(text)
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

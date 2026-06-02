from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, desc, select
from starlette.status import HTTP_303_SEE_OTHER

from .auth import add_session_middleware, current_user, login_user, logout_user, verify_login
from .config import ensure_data_dirs, settings
from .db import get_session, init_db
from .extractors import extract_candidates, hash_content, hash_file
from .models import (
    CandidateStatus,
    FundNav,
    FundTransaction,
    FundTransactionCandidate,
    TransactionAction,
)
from .nav import sync_nav_for_fund
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
    if file and file.filename:
        content = await file.read()
        safe_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{Path(file.filename).name}"
        target = settings.uploads_dir / safe_name
        target.write_bytes(content)
        source_file = str(target)
        source_hash = hash_file(target)

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
    session.commit()
    return redirect("/candidates")


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

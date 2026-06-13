import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, desc, select
from starlette.status import HTTP_303_SEE_OTHER

from .app_settings import configured, masked, runtime_settings, save_settings
from .auth import add_session_middleware, current_user, login_user, logout_user, verify_login
from .candidate_issues import issue, set_candidate_issues
from .config import ensure_data_dirs, settings
from .db import engine, get_session, init_db
from .fund_rule_sync import fetch_fund_rule_from_akshare, search_fund_by_name
from .models import (
    AppSetting,
    CandidateStatus,
    CandidateIssue,
    EventType,
    FundAlias,
    FundEvent,
    FundFeeTier,
    FundNav,
    FundRule,
    FundTransaction,
    FundType,
    ImportDocument,
    ImportStatus,
    OcrRow,
    RowStatus,
    TransactionAction,
    TransactionCandidate,
)
from .nav import sync_nav_for_fund
from .ocr import recognize_file
from .templates import templates
from .timezone import BUSINESS_TIMEZONE_NAME, now_shanghai_naive


app = FastAPI(title="Fund Ledger")
add_session_middleware(app)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

FUND_MAP_PATH = Path(__file__).parent.parent / "fund_map.xlsx"
SUCCESS_WORDS = ("成功", "已确认", "确认成功", "已完成", "确认中")
CANCEL_WORDS = ("撤销", "撤单", "取消", "已撤销", "已取消")
FAIL_WORDS = ("失败", "未成功")
BUY_WORDS = ("买入", "申购", "认购", "购买", "定投", "定期定额", "buy")
SELL_WORDS = ("卖出", "赎回", "转换", "基金转换", "转换出", "sell")
DIVIDEND_REINVEST_WORDS = ("红利再投", "红利再投资", "分红再投资")
DIVIDEND_WORDS = ("现金分红", "分红")
DIVIDEND_METHOD_WORDS = ("修改分红方式", "分红方式")
SIP_START_WORDS = ("开始定投", "新增定投", "开通定投")
SIP_STOP_WORDS = ("停止定投", "终止定投", "暂停定投")
SIP_UPDATE_WORDS = ("修改定投", "变更定投")
FORCED_ADJUST_WORDS = ("强制调增", "强制调减", "份额调增", "份额调减")
TABLE_HEADER_WORDS = ("名称", "创建时间", "交易类型", "交易渠道", "份额", "金额", "状态")
PLACEHOLDER_VALUES = {"", "-", "--", "—", "－", "无", "暂无"}


@app.on_event("startup")
def on_startup() -> None:
    ensure_data_dirs()
    init_db()
    with Session(engine) as session:
        seed_aliases_from_fund_map(session)


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=HTTP_303_SEE_OTHER)


def require_user(request: Request) -> str:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    return user


@app.exception_handler(401)
def auth_exception(request: Request, exc: HTTPException) -> RedirectResponse:
    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    return redirect(f"/login?next={quote(next_path, safe='')}")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "next": next})


@app.post("/login")
def login_submit(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("")):
    if verify_login(username, password):
        login_user(request, username)
        return redirect(next or "/")
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "next": next, "error": "用户名或密码错误"},
        status_code=401,
    )


@app.post("/logout")
def logout(request: Request):
    logout_user(request)
    return redirect("/login")


@app.get("/", response_class=HTMLResponse)
def root(_: str = Depends(require_user)):
    return redirect("/candidates")


@app.get("/performance")
def performance_redirect(_: str = Depends(require_user)):
    return redirect("/holdings")


@app.get("/portfolio")
def portfolio_redirect(_: str = Depends(require_user)):
    return redirect("/holdings")


@app.get("/ledger")
def ledger_redirect(_: str = Depends(require_user)):
    return redirect("/transactions")


@app.get("/ocr-export")
def ocr_export_redirect(_: str = Depends(require_user)):
    return redirect("/imports")


@app.get("/nav")
def nav_redirect(_: str = Depends(require_user)):
    return redirect("/funds")


@app.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, _: str = Depends(require_user)):
    return templates.TemplateResponse("upload.html", {"request": request})


@app.post("/upload")
def upload_submit(
    raw_text: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    file: UploadFile | None = File(None),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    ensure_data_dirs()
    uploads = [item for item in files if item and item.filename]
    if file and file.filename:
        uploads.append(file)
    documents: list[ImportDocument] = []
    for upload in uploads:
        doc = save_upload_document(session, upload)
        documents.append(doc)
    if raw_text.strip():
        doc = ImportDocument(
            file_name="粘贴文本",
            source_hash=_hash_content(raw_text.encode()),
            ocr_text=raw_text,
            status=ImportStatus.ocr_done,
        )
        session.add(doc)
        documents.append(doc)
    session.commit()
    if len(documents) == 1:
        session.refresh(documents[0])
        return redirect(f"/imports/{documents[0].id}")
    return redirect(f"/imports?message=已上传 {len(documents)} 个导入")


@app.post("/upload/single")
def upload_single(
    file: UploadFile = File(...),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    if not file.filename:
        return JSONResponse({"ok": False, "error": "空文件名"}, status_code=400)
    doc = save_upload_document(session, file)
    session.commit()
    session.refresh(doc)
    return {"ok": True, "document_id": doc.id, "file_name": doc.file_name}


@app.get("/imports", response_class=HTMLResponse)
def imports_page(
    request: Request,
    message: str = "",
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    docs = session.exec(select(ImportDocument).order_by(desc(ImportDocument.created_at))).all()
    return templates.TemplateResponse(
        "imports.html",
        {
            "request": request,
            "message": message,
            "documents": docs,
            "total": len(docs),
            "pending": sum(1 for d in docs if d.status == ImportStatus.uploaded),
            "ocr_done": sum(1 for d in docs if d.status in {ImportStatus.ocr_done, ImportStatus.parsed}),
            "ocr_doc_ids": [d.id for d in docs if d.status == ImportStatus.uploaded and d.source_file],
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
    doc = session.get(ImportDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404)
    rows = session.exec(select(OcrRow).where(OcrRow.document_id == document_id).order_by(OcrRow.row_index)).all()
    candidates = session.exec(
        select(TransactionCandidate).where(TransactionCandidate.document_id == document_id).order_by(TransactionCandidate.id)
    ).all()
    stats = candidate_stats(candidates)
    return templates.TemplateResponse(
        "import_detail.html",
        {"request": request, "document": doc, "rows": rows, "candidates": candidates, "stats": stats, "message": message},
    )


@app.post("/imports/{document_id}/ocr")
def import_run_ocr(
    document_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    doc = session.get(ImportDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404)
    result = run_ocr_for_document(session, doc)
    if not result["ok"]:
        return redirect(f"/imports/{document_id}?message=OCR 失败: {result['error']}")
    return redirect(
        f"/imports/{document_id}?message=OCR 完成，保存 {result['rows']} 行，生成 {result['candidates']} 个候选，"
        f"自动入账 {result['posted']} 条，事件 {result['events']} 条，跳过 {result['skipped']} 条"
    )


@app.post("/imports/{document_id}/rerun-ocr")
def import_rerun_ocr(
    document_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    doc = session.get(ImportDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404)
    blocked = clear_import_outputs(session, document_id, include_rows=True)
    if blocked:
        return redirect(f"/imports/{document_id}?message={blocked}")
    result = run_ocr_for_document(session, doc)
    if not result["ok"]:
        return redirect(f"/imports/{document_id}?message=重跑 OCR 失败: {result['error']}")
    return redirect(
        f"/imports/{document_id}?message=已重跑 OCR，保存 {result['rows']} 行，生成 {result['candidates']} 个候选，"
        f"自动入账 {result['posted']} 条，事件 {result['events']} 条，跳过 {result['skipped']} 条"
    )


@app.post("/imports/{document_id}/ocr-json")
def import_run_ocr_json(
    document_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    doc = session.get(ImportDocument, document_id)
    if not doc:
        return JSONResponse({"ok": False, "error": "导入不存在", "document_id": document_id}, status_code=404)
    result = run_ocr_for_document(session, doc)
    return result


@app.post("/imports/{document_id}/delete")
def import_delete(
    document_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    doc = session.get(ImportDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404)
    source_file = doc.source_file
    blocked = clear_import_outputs(session, document_id, include_rows=True)
    if blocked:
        return redirect(f"/imports/{document_id}?message={blocked}")
    session.delete(doc)
    session.commit()
    if source_file:
        try:
            Path(source_file).unlink(missing_ok=True)
        except OSError:
            pass
    return redirect("/imports?message=导入文档已删除")


@app.post("/imports/{document_id}/parse")
def import_parse(
    document_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    doc = session.get(ImportDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404)
    if doc.ocr_text and not session.exec(select(OcrRow).where(OcrRow.document_id == document_id)).first():
        rows = [[cell for cell in line.split() if cell] for line in doc.ocr_text.splitlines() if line.strip()]
        save_ocr_rows(session, doc, rows)
    blocked = clear_import_outputs(session, document_id, include_rows=False)
    if blocked:
        return redirect(f"/imports/{document_id}?message={blocked}")
    created = parse_document_candidates(session, document_id)
    return redirect(f"/imports/{document_id}?message=已生成 {created} 个候选")


@app.post("/imports/{document_id}/auto-post")
def import_auto_post(
    document_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    if not session.get(ImportDocument, document_id):
        raise HTTPException(status_code=404)
    candidates = session.exec(
        select(TransactionCandidate)
        .where(
            TransactionCandidate.document_id == document_id,
            TransactionCandidate.status.in_([CandidateStatus.auto_ready, CandidateStatus.pending, CandidateStatus.event]),
        )
        .order_by(TransactionCandidate.id)
    ).all()
    posted, events, skipped = auto_post_candidates(session, candidates)
    return redirect(f"/imports/{document_id}?message=本文件自动入账 {posted} 条，事件 {events} 条，跳过 {skipped} 条")


@app.get("/candidates", response_class=HTMLResponse)
def candidates_page(
    request: Request,
    status: str = "",
    message: str = "",
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    query = select(TransactionCandidate).order_by(TransactionCandidate.id.desc())
    if status in CandidateStatus._value2member_map_:
        query = query.where(TransactionCandidate.status == CandidateStatus(status))
    candidates = session.exec(query).all()
    candidate_ids = [c.id for c in candidates if c.id]
    issue_rows = (
        session.exec(select(CandidateIssue).where(CandidateIssue.candidate_id.in_(candidate_ids)).order_by(CandidateIssue.candidate_id, CandidateIssue.id)).all()
        if candidate_ids
        else []
    )
    issues_by_candidate: dict[int, list[CandidateIssue]] = {}
    for row in issue_rows:
        issues_by_candidate.setdefault(row.candidate_id, []).append(row)
    auto_ready = [c for c in candidates if candidate_is_auto_ready(c)]
    return templates.TemplateResponse(
        "candidates.html",
        {
            "request": request,
            "candidates": candidates,
            "issues_by_candidate": issues_by_candidate,
            "auto_ready_count": len(auto_ready),
            "message": message,
            "status_filter": status,
        },
    )


@app.post("/candidates/auto-post")
def candidates_auto_post(
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    candidates = session.exec(
        select(TransactionCandidate)
        .where(TransactionCandidate.status.in_([CandidateStatus.auto_ready, CandidateStatus.pending, CandidateStatus.event]))
        .order_by(TransactionCandidate.id)
    ).all()
    posted, events, skipped = auto_post_candidates(session, candidates)
    return redirect(f"/candidates?message=自动入账 {posted} 条，事件 {events} 条，跳过 {skipped} 条")


@app.post("/candidates/sync-nav")
def candidates_sync_nav(
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    candidates = session.exec(
        select(TransactionCandidate)
        .where(TransactionCandidate.status.in_([CandidateStatus.needs_review, CandidateStatus.pending, CandidateStatus.auto_ready]))
        .order_by(TransactionCandidate.id)
    ).all()
    nav_sync_attempts: set[str] = set()
    rule_sync_attempts: set[str] = set()
    auto_before = sum(1 for candidate in candidates if candidate_is_auto_ready(candidate))
    for candidate in candidates:
        normalize_candidate_for_posting(session, candidate, nav_sync_attempts, rule_sync_attempts)
        session.add(candidate)
    session.commit()
    refreshed = len(candidates)
    auto_after = sum(1 for candidate in candidates if candidate_is_auto_ready(candidate))
    return redirect(f"/candidates?message=已补净值并重算 {refreshed} 条候选，可自动入账 {auto_before} → {auto_after} 条")


@app.post("/candidates/{candidate_id}/update")
def candidate_update(
    candidate_id: int,
    fund_code: str = Form(""),
    fund_name: str = Form(""),
    fund_type: str = Form("unknown"),
    action: str = Form(""),
    row_status: str = Form(""),
    trade_date: str = Form(""),
    submitted_at: str = Form(""),
    amount_cny: str = Form(""),
    share: str = Form(""),
    nav: str = Form(""),
    fee: str = Form(""),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    candidate = session.get(TransactionCandidate, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404)
    apply_candidate_form(candidate, fund_code, fund_name, fund_type, action, row_status, trade_date, submitted_at, amount_cny, share, nav, fee)
    if candidate.fund_code and candidate.fund_name:
        upsert_alias(session, candidate.fund_name, candidate.fund_code, candidate.fund_name, candidate.fund_type, "manual")
    normalize_candidate_for_posting(session, candidate)
    session.add(candidate)
    session.commit()
    return redirect(f"/candidates?message=候选 #{candidate_id} 已保存")


@app.post("/candidates/{candidate_id}/post")
def candidate_post(
    candidate_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    candidate = session.get(TransactionCandidate, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404)
    normalize_candidate_for_posting(session, candidate)
    if candidate.event_type:
        post_event(session, candidate)
    elif candidate_is_auto_ready(candidate):
        post_candidate(session, candidate)
    else:
        candidate.status = CandidateStatus.needs_review
        session.add(candidate)
        session.commit()
        return redirect(f"/candidates?message=候选 #{candidate_id} 仍需修正: {candidate.review_reason}")
    session.commit()
    return redirect(f"/candidates?message=候选 #{candidate_id} 已入账")


@app.post("/candidates/{candidate_id}/ignore")
def candidate_ignore(candidate_id: int, _: str = Depends(require_user), session: Session = Depends(get_session)):
    candidate = session.get(TransactionCandidate, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404)
    candidate.status = CandidateStatus.ignored
    candidate.updated_at = now_shanghai_naive()
    session.add(candidate)
    session.commit()
    return redirect("/candidates?message=已忽略")


@app.get("/transactions", response_class=HTMLResponse)
def transactions_page(
    request: Request,
    fund_code: str = "",
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    query = select(FundTransaction).order_by(desc(FundTransaction.trade_date), desc(FundTransaction.id))
    if fund_code:
        query = query.where(FundTransaction.fund_code == fund_code.zfill(6))
    txs = session.exec(query).all()
    by_fund: dict[str, list[FundTransaction]] = {}
    for tx in txs:
        by_fund.setdefault(tx.fund_code, []).append(tx)
    return templates.TemplateResponse("transactions.html", {"request": request, "by_fund": by_fund, "fund_code_filter": fund_code})


@app.get("/events", response_class=HTMLResponse)
def events_page(request: Request, _: str = Depends(require_user), session: Session = Depends(get_session)):
    events = session.exec(select(FundEvent).order_by(desc(FundEvent.created_at))).all()
    return templates.TemplateResponse("events.html", {"request": request, "events": events})


@app.get("/holdings", response_class=HTMLResponse)
def holdings_page(request: Request, _: str = Depends(require_user), session: Session = Depends(get_session)):
    positions = calculate_positions(session)
    active = [p for p in positions if not p["is_closed"]]
    closed = [p for p in positions if p["is_closed"]]
    totals = {
        "market_value": sum(p["market_value"] for p in active),
        "cost": sum(p["cost"] for p in active),
        "profit": sum(p["profit"] for p in active),
        "realized": sum(p["realized_profit"] for p in positions),
    }
    return templates.TemplateResponse(
        "holdings.html",
        {"request": request, "holdings": active, "closed": closed, "totals": totals},
    )


@app.get("/funds/{fund_code}", response_class=HTMLResponse)
def fund_detail_page(fund_code: str, request: Request, _: str = Depends(require_user), session: Session = Depends(get_session)):
    code = fund_code.zfill(6)
    txs = session.exec(select(FundTransaction).where(FundTransaction.fund_code == code).order_by(FundTransaction.trade_date, FundTransaction.id)).all()
    if not txs:
        raise HTTPException(status_code=404)
    navs = session.exec(select(FundNav).where(FundNav.fund_code == code).order_by(FundNav.nav_date)).all()
    chart = build_fund_chart(txs, navs)
    position = next((p for p in calculate_positions(session, include_closed=True) if p["fund_code"] == code), None)
    return templates.TemplateResponse(
        "fund_detail.html",
        {"request": request, "fund_code": code, "fund_name": txs[-1].fund_name, "transactions": txs, "chart": chart, "position": position},
    )


@app.get("/funds", response_class=HTMLResponse)
def funds_page(request: Request, _: str = Depends(require_user), session: Session = Depends(get_session)):
    aliases = session.exec(select(FundAlias).order_by(FundAlias.keyword)).all()
    rules = session.exec(select(FundRule).order_by(FundRule.fund_code)).all()
    return templates.TemplateResponse("funds.html", {"request": request, "aliases": aliases, "rules": rules})


@app.post("/funds/alias")
def fund_alias_save(
    keyword: str = Form(...),
    fund_code: str = Form(...),
    fund_name: str = Form(""),
    fund_type: str = Form("unknown"),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    upsert_alias(session, keyword, fund_code, fund_name or keyword, FundType(fund_type), "manual")
    ensure_rule(session, fund_code, fund_name or keyword, FundType(fund_type))
    session.commit()
    return redirect("/funds?message=映射已保存")


@app.post("/fund-rules")
def fund_rule_save(
    fund_code: str = Form(...),
    fund_name: str = Form(""),
    fund_type: str = Form("unknown"),
    buy_confirm_days: int = Form(1),
    sell_confirm_days: int = Form(1),
    cutoff_time: str = Form("15:00"),
    buy_fee_rate: float = Form(0.0),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    rule = ensure_rule(session, fund_code, fund_name, FundType(fund_type))
    rule.buy_confirm_days = buy_confirm_days
    rule.sell_confirm_days = sell_confirm_days
    rule.cutoff_time = cutoff_time
    rule.buy_fee_rate = buy_fee_rate
    rule.updated_at = now_shanghai_naive()
    session.add(rule)
    session.commit()
    return redirect("/funds?message=规则已保存")


@app.post("/fund-rules/{fund_code}/sync")
def fund_rule_sync(fund_code: str, _: str = Depends(require_user), session: Session = Depends(get_session)):
    rule = ensure_rule(session, fund_code, sync=True)
    session.commit()
    return redirect(f"/funds?message=规则已同步: {rule.fund_code}")


@app.post("/funds/{fund_code}/sync-nav")
def fund_nav_sync(fund_code: str, _: str = Depends(require_user), session: Session = Depends(get_session)):
    inserted, error = sync_nav_for_fund(session, fund_code.zfill(6))
    if error:
        return redirect(f"/funds?message=净值同步失败: {error}")
    return redirect(f"/funds?message=净值已同步 {inserted} 条")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, message: str = "", _: str = Depends(require_user), session: Session = Depends(get_session)):
    config = runtime_settings(session)
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "config": config, "message": message, "configured": configured, "masked": masked},
    )


@app.post("/settings")
def settings_update(
    ocr_enabled: str = Form("off"),
    baidu_ocr_api_key: str = Form(""),
    baidu_ocr_secret_key: str = Form(""),
    baidu_table_ocr_endpoint: str = Form(""),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    current = runtime_settings(session)
    save_settings(
        session,
        {
            "OCR_ENABLED": "true" if ocr_enabled == "on" else "false",
            "OCR_BACKEND": "baidu_table",
            "BAIDU_OCR_API_KEY": preserve_masked_secret(baidu_ocr_api_key, current.get("BAIDU_OCR_API_KEY", "")),
            "BAIDU_OCR_SECRET_KEY": preserve_masked_secret(baidu_ocr_secret_key, current.get("BAIDU_OCR_SECRET_KEY", "")),
            "BAIDU_TABLE_OCR_ENDPOINT": baidu_table_ocr_endpoint.strip()
            or current.get("BAIDU_TABLE_OCR_ENDPOINT", "https://aip.baidubce.com/rest/2.0/ocr/v1/table"),
        },
    )
    return redirect("/settings?message=设置已保存")


def save_ocr_rows(session: Session, doc: ImportDocument, rows: list[list[str]]) -> int:
    existing = {
        (row.row_index, row.row_hash)
        for row in session.exec(select(OcrRow).where(OcrRow.document_id == doc.id)).all()
    }
    count = 0
    for index, row in enumerate(rows):
        raw_json = json.dumps(row, ensure_ascii=False)
        raw_text = " ".join(str(cell).strip() for cell in row if str(cell).strip())
        row_hash = _hash_content(raw_json.encode())
        if (index, row_hash) in existing:
            continue
        session.add(OcrRow(document_id=doc.id, row_index=index, row_hash=row_hash, raw_json=raw_json, raw_text=raw_text))
        count += 1
    session.commit()
    return count


def save_upload_document(session: Session, upload: UploadFile) -> ImportDocument:
    ensure_data_dirs()
    content = upload.file.read()
    safe_name = f"{now_shanghai_naive().strftime('%Y%m%d%H%M%S%f')}-{Path(upload.filename or 'upload').name}"
    target = settings.uploads_dir / safe_name
    target.write_bytes(content)
    doc = ImportDocument(
        file_name=upload.filename,
        source_file=str(target),
        source_hash=_hash_content(content),
        content_type=upload.content_type,
        status=ImportStatus.uploaded,
    )
    session.add(doc)
    return doc


def run_ocr_for_document(session: Session, doc: ImportDocument, auto_post: bool = True) -> dict:
    if not doc.source_file:
        return {"ok": False, "document_id": doc.id, "file_name": doc.file_name, "error": "没有可 OCR 的文件"}
    config = runtime_settings(session)
    doc.status = ImportStatus.ocr_running
    doc.updated_at = now_shanghai_naive()
    session.add(doc)
    session.commit()
    try:
        result = recognize_file(doc.source_file, config)
    except Exception as exc:
        doc.status = ImportStatus.error
        doc.error_message = str(exc)
        doc.updated_at = now_shanghai_naive()
        session.add(doc)
        session.commit()
        return {"ok": False, "document_id": doc.id, "file_name": doc.file_name, "error": str(exc)}
    doc.ocr_text = result.text
    doc.status = ImportStatus.ocr_done
    doc.error_message = ""
    doc.updated_at = now_shanghai_naive()
    session.add(doc)
    session.commit()
    row_count = save_ocr_rows(session, doc, result.rows)
    candidate_count = parse_document_candidates(session, doc.id)
    posted = events = skipped = 0
    if auto_post:
        candidates = session.exec(
            select(TransactionCandidate)
            .where(
                TransactionCandidate.document_id == doc.id,
                TransactionCandidate.status.in_([CandidateStatus.auto_ready, CandidateStatus.pending, CandidateStatus.event]),
            )
            .order_by(TransactionCandidate.id)
        ).all()
        posted, events, skipped = auto_post_candidates(session, candidates)
    return {
        "ok": True,
        "document_id": doc.id,
        "file_name": doc.file_name,
        "rows": row_count,
        "candidates": candidate_count,
        "posted": posted,
        "events": events,
        "skipped": skipped,
    }


def clear_import_outputs(session: Session, document_id: int, include_rows: bool = False) -> str:
    candidates = session.exec(select(TransactionCandidate).where(TransactionCandidate.document_id == document_id)).all()
    if any(candidate.posted_transaction_id or candidate.posted_event_id for candidate in candidates):
        return "已有候选入账或写入事件，不能直接删除；请先在候选/流水中处理对应记录"
    candidate_ids = [candidate.id for candidate in candidates if candidate.id]
    if candidate_ids:
        for item in session.exec(select(CandidateIssue).where(CandidateIssue.candidate_id.in_(candidate_ids))).all():
            session.delete(item)
    for candidate in candidates:
        session.delete(candidate)
    if include_rows:
        for row in session.exec(select(OcrRow).where(OcrRow.document_id == document_id)).all():
            session.delete(row)
    session.commit()
    return ""


def parse_document_candidates(session: Session, document_id: int) -> int:
    rows = session.exec(select(OcrRow).where(OcrRow.document_id == document_id).order_by(OcrRow.row_index)).all()
    existing = {
        (c.ocr_row_id, c.row_hash)
        for c in session.exec(select(TransactionCandidate).where(TransactionCandidate.document_id == document_id)).all()
    }
    created = 0
    nav_sync_attempts: set[str] = set()
    rule_sync_attempts: set[str] = set()
    for row in rows:
        if (row.id, row.row_hash) in existing:
            continue
        if is_header_ocr_row(row):
            continue
        candidate = parse_ocr_row(session, row)
        session.add(candidate)
        session.flush()
        normalize_candidate_for_posting(session, candidate, nav_sync_attempts, rule_sync_attempts)
        created += 1
    doc = session.get(ImportDocument, document_id)
    if doc:
        doc.status = ImportStatus.parsed
        doc.updated_at = now_shanghai_naive()
        session.add(doc)
    session.commit()
    return created


def parse_ocr_row(session: Session, row: OcrRow) -> TransactionCandidate:
    cells = [str(cell).strip() for cell in json.loads(row.raw_json)]
    text = row.raw_text
    table_row = is_table_transaction_row(cells)
    row_status = classify_row_status(table_status_cell(cells) if table_row else text)
    action_text = table_action_cell(cells) if table_row else text
    action, event_type = classify_action(action_text)
    trade_date, submitted_at = extract_datetime(table_datetime_cell(cells) if table_row else text)
    amount = extract_amount(cells, text)
    share = extract_share(cells, text)
    fund_name = extract_fund_name(session, cells, text)
    alias = match_alias(session, fund_name) if fund_name else None
    fund_code = alias.fund_code if alias else ""
    fund_type = alias.fund_type if alias else infer_fund_type(fund_name, fund_code)
    if fund_code:
        ensure_rule(session, fund_code, alias.fund_name if alias else fund_name, fund_type)
    candidate = TransactionCandidate(
        document_id=row.document_id,
        ocr_row_id=row.id,
        row_hash=row.row_hash,
        row_status=row_status,
        action=action,
        event_type=event_type,
        fund_code=fund_code,
        fund_name=candidate_fund_name(fund_name, alias),
        fund_type=fund_type,
        trade_date=trade_date,
        submitted_at=submitted_at,
        amount_cny=amount,
        share=share,
        raw_text=text,
        confidence=0.7,
    )
    return candidate


def candidate_fund_name(ocr_name: str, alias: FundAlias | None) -> str:
    cleaned = normalize_fund_name(ocr_name)
    if not alias:
        return cleaned
    if alias.source == "fund_map":
        return cleaned
    return normalize_fund_name(alias.fund_name) if alias.fund_name else cleaned


def normalize_candidate_for_posting(
    session: Session,
    candidate: TransactionCandidate,
    nav_sync_attempts: set[str] | None = None,
    rule_sync_attempts: set[str] | None = None,
) -> None:
    issues = []
    if candidate.action is None and candidate.event_type is None and candidate.raw_text:
        candidate.action, candidate.event_type = classify_action(candidate.raw_text)
    if candidate.event_type:
        candidate.status = CandidateStatus.event
        set_candidate_issues(session, candidate, [issue("non_money_event", "非资金事件", "info")])
        return
    if candidate.row_status != RowStatus.success:
        if candidate.row_status in {RowStatus.cancelled, RowStatus.failed}:
            candidate.event_type = EventType.ignored_status
            candidate.status = CandidateStatus.event
        else:
            candidate.status = CandidateStatus.needs_review
        set_candidate_issues(session, candidate, [issue("row_status_not_success", "状态不是成功")])
        return
    if not candidate.fund_code:
        issues.append(issue("missing_fund_code", "缺基金代码"))
    if candidate.fund_type == FundType.unknown:
        issues.append(issue("missing_fund_type", "缺基金类型"))
    if not candidate.action:
        issues.append(issue("missing_action", "缺动作"))
    if not candidate.trade_date:
        issues.append(issue("missing_trade_date", "缺交易日期"))
    if candidate.action == TransactionAction.buy and candidate.amount_cny is None:
        issues.append(issue("buy_missing_amount", "买入缺金额"))
    if candidate.action == TransactionAction.sell and candidate.share is None:
        issues.append(issue("sell_missing_share", "卖出缺份额"))
    if candidate.action == TransactionAction.dividend and candidate.amount_cny is None:
        issues.append(issue("dividend_missing_amount", "分红缺金额"))
    if candidate.action == TransactionAction.dividend_reinvest and candidate.share is None and candidate.amount_cny is None:
        issues.append(issue("reinvest_missing_share_or_amount", "红利再投缺份额或金额"))
    if candidate.fund_type in {FundType.etf, FundType.money_fund}:
        candidate.status = CandidateStatus.auto_ready if not issues else CandidateStatus.needs_review
        set_candidate_issues(session, candidate, issues)
        return
    if candidate.fund_type == FundType.open_fund and candidate.fund_code and candidate.trade_date:
        rule = ensure_rule(session, candidate.fund_code, candidate.fund_name, candidate.fund_type)
        if candidate.action == TransactionAction.sell:
            rule = ensure_sell_fee_rule(session, candidate, rule, rule_sync_attempts)
            if not has_sell_fee_tiers(session, candidate.fund_code):
                issues.append(issue("missing_sell_fee_rule", "缺赎回费率规则"))
        nav, nav_sync_error = effective_nav_with_auto_sync(session, candidate, rule, nav_sync_attempts)
        if nav:
            candidate.nav = candidate.nav or nav.unit_nav
            candidate.effective_nav_date = nav.nav_date
            candidate.confirm_date = nth_nav_date(session, candidate.fund_code, nav.nav_date, rule.buy_confirm_days if candidate.action == TransactionAction.buy else rule.sell_confirm_days)
        else:
            issues.append(issue("missing_effective_nav", "缺有效净值", detail=nav_sync_error or "本地无覆盖交易日的净值"))
    if candidate.action == TransactionAction.buy and candidate.amount_cny is not None and candidate.nav and candidate.share is None:
        rule = ensure_rule(session, candidate.fund_code, candidate.fund_name, candidate.fund_type)
        candidate.fee = candidate.fee if candidate.fee is not None else round(candidate.amount_cny * rule.buy_fee_rate, 2)
        candidate.share = round((candidate.amount_cny - (candidate.fee or 0)) / candidate.nav, 4)
    if candidate.action == TransactionAction.sell and candidate.share is not None and candidate.nav:
        gross_amount = round(candidate.share * candidate.nav, 2)
        estimated_fee = estimate_sell_fee(session, candidate, gross_amount)
        should_recompute_fee = candidate.fee is None or (candidate.fee == 0 and estimated_fee > 0)
        if should_recompute_fee:
            candidate.fee = estimated_fee
        if candidate.amount_cny is None or abs(candidate.amount_cny - gross_amount) < 0.01:
            candidate.amount_cny = round(gross_amount - (candidate.fee or 0), 2)
    candidate.status = CandidateStatus.auto_ready if not issues else CandidateStatus.needs_review
    set_candidate_issues(session, candidate, issues)
    candidate.updated_at = now_shanghai_naive()


def candidate_is_auto_ready(candidate: TransactionCandidate) -> bool:
    if candidate.status == CandidateStatus.posted:
        return False
    if candidate.event_type:
        return True
    if candidate.status != CandidateStatus.auto_ready:
        return False
    return (
        candidate.row_status == RowStatus.success
        and bool(candidate.fund_code)
        and candidate.fund_type != FundType.unknown
        and candidate.action is not None
        and candidate.trade_date is not None
        and (candidate.fund_type in {FundType.etf, FundType.money_fund} or candidate.nav is not None)
        and ((candidate.action == TransactionAction.buy and candidate.amount_cny is not None) or (candidate.action == TransactionAction.sell and candidate.share is not None) or candidate.action in {TransactionAction.dividend, TransactionAction.dividend_reinvest})
    )


def candidate_stats(candidates: list[TransactionCandidate]) -> dict[str, int]:
    return {
        "total": len(candidates),
        "auto_ready": sum(1 for candidate in candidates if candidate.status == CandidateStatus.auto_ready),
        "needs_review": sum(1 for candidate in candidates if candidate.status == CandidateStatus.needs_review),
        "event": sum(1 for candidate in candidates if candidate.status == CandidateStatus.event),
        "posted": sum(1 for candidate in candidates if candidate.status == CandidateStatus.posted),
    }


def auto_post_candidates(session: Session, candidates: list[TransactionCandidate]) -> tuple[int, int, int]:
    posted = 0
    skipped = 0
    events = 0
    nav_sync_attempts: set[str] = set()
    rule_sync_attempts: set[str] = set()
    for candidate in candidates:
        if candidate.status == CandidateStatus.posted:
            continue
        if candidate.status == CandidateStatus.event or candidate.event_type:
            post_event(session, candidate)
            events += 1
            continue
        normalize_candidate_for_posting(session, candidate, nav_sync_attempts, rule_sync_attempts)
        if candidate_is_auto_ready(candidate):
            post_candidate(session, candidate)
            posted += 1
        else:
            candidate.status = CandidateStatus.needs_review
            session.add(candidate)
            skipped += 1
    session.commit()
    return posted, events, skipped


def post_candidate(session: Session, candidate: TransactionCandidate) -> FundTransaction:
    if candidate.posted_transaction_id:
        existing = session.get(FundTransaction, candidate.posted_transaction_id)
        if existing:
            return existing
    tx = FundTransaction(
        candidate_id=candidate.id,
        fund_code=candidate.fund_code,
        fund_name=candidate.fund_name,
        fund_type=candidate.fund_type,
        trade_date=candidate.trade_date,
        submitted_at=candidate.submitted_at,
        effective_nav_date=candidate.effective_nav_date,
        confirm_date=candidate.confirm_date,
        action=candidate.action,
        amount_cny=candidate.amount_cny,
        share=candidate.share,
        nav=candidate.nav,
        fee=candidate.fee,
        raw_text=candidate.raw_text,
    )
    session.add(tx)
    session.flush()
    candidate.status = CandidateStatus.posted
    candidate.posted_transaction_id = tx.id
    candidate.updated_at = now_shanghai_naive()
    session.add(candidate)
    return tx


def post_event(session: Session, candidate: TransactionCandidate) -> FundEvent:
    if candidate.posted_event_id:
        existing = session.get(FundEvent, candidate.posted_event_id)
        if existing:
            return existing
    event = FundEvent(
        candidate_id=candidate.id,
        event_type=candidate.event_type or EventType.other,
        fund_code=candidate.fund_code,
        fund_name=candidate.fund_name,
        fund_type=candidate.fund_type,
        event_date=candidate.trade_date,
        submitted_at=candidate.submitted_at,
        amount_cny=candidate.amount_cny,
        raw_text=candidate.raw_text,
        note=candidate.review_reason,
    )
    session.add(event)
    session.flush()
    if event.event_type == EventType.dividend_method and candidate.fund_code:
        rule = ensure_rule(session, candidate.fund_code, candidate.fund_name, candidate.fund_type)
        rule.dividend_method = candidate.raw_text
        rule.updated_at = now_shanghai_naive()
        session.add(rule)
    candidate.status = CandidateStatus.event
    candidate.posted_event_id = event.id
    candidate.updated_at = now_shanghai_naive()
    session.add(candidate)
    return event


def classify_row_status(text: str) -> RowStatus:
    if any(word in text for word in CANCEL_WORDS):
        return RowStatus.cancelled
    if any(word in text for word in FAIL_WORDS):
        return RowStatus.failed
    if any(word in text for word in SUCCESS_WORDS):
        return RowStatus.success
    return RowStatus.unknown


def classify_action(text: str) -> tuple[TransactionAction | None, EventType | None]:
    if any(word in text for word in DIVIDEND_METHOD_WORDS):
        return None, EventType.dividend_method
    if any(word in text for word in SIP_START_WORDS):
        return None, EventType.sip_start
    if any(word in text for word in SIP_STOP_WORDS):
        return None, EventType.sip_stop
    if any(word in text for word in SIP_UPDATE_WORDS):
        return None, EventType.sip_update
    if any(word in text for word in FORCED_ADJUST_WORDS):
        return None, EventType.other
    if any(word in text for word in DIVIDEND_REINVEST_WORDS):
        return TransactionAction.dividend_reinvest, None
    if any(word in text for word in SELL_WORDS):
        return TransactionAction.sell, None
    if any(word in text for word in BUY_WORDS):
        return TransactionAction.buy, None
    if any(word in text for word in DIVIDEND_WORDS):
        return TransactionAction.dividend, None
    return None, None


def is_header_ocr_row(row: OcrRow) -> bool:
    try:
        cells = [str(cell).strip() for cell in json.loads(row.raw_json)]
    except (TypeError, json.JSONDecodeError):
        cells = []
    text = row.raw_text
    return len(cells) >= 5 and sum(1 for word in TABLE_HEADER_WORDS if word in text) >= 5


def is_table_transaction_row(cells: list[str]) -> bool:
    if len(cells) < 7:
        return False
    action = table_action_cell(cells)
    status = table_status_cell(cells)
    return bool(action and status and (classify_action(action)[0] or classify_action(action)[1] or classify_row_status(status) != RowStatus.unknown))


def normalized_cell(cell: str | None) -> str:
    return str(cell or "").strip()


def table_datetime_cell(cells: list[str]) -> str:
    return normalized_cell(cells[1]) if is_table_transaction_row_shape(cells) else ""


def table_action_cell(cells: list[str]) -> str:
    return normalized_cell(cells[2]) if len(cells) >= 3 else ""


def table_share_cell(cells: list[str]) -> str:
    return normalized_cell(cells[4]) if is_table_transaction_row_shape(cells) else ""


def table_amount_cell(cells: list[str]) -> str:
    return normalized_cell(cells[5]) if is_table_transaction_row_shape(cells) else ""


def table_status_cell(cells: list[str]) -> str:
    if is_table_transaction_row_shape(cells):
        return normalized_cell(cells[7] if len(cells) >= 8 else cells[-1])
    return ""


def is_table_transaction_row_shape(cells: list[str]) -> bool:
    return len(cells) >= 7 and bool(normalized_cell(cells[0])) and bool(normalized_cell(cells[1])) and bool(normalized_cell(cells[2]))


def parse_float_cell(cell: str | None) -> float | None:
    value = normalized_cell(cell)
    if value in PLACEHOLDER_VALUES:
        return None
    return parse_float(value)


def extract_datetime(text: str) -> tuple[date | None, time | None]:
    normalized = text.replace("/", "-").replace(".", "-")
    match = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})(?:\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?", normalized)
    if not match:
        match = re.search(r"(20\d{2})(\d{2})(\d{2})(?:\s*(\d{1,2}):?(\d{2}):?(\d{2})?)?", normalized)
    if not match:
        return None, None
    y, m, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
    hh = int(match.group(4) or 0)
    mm = int(match.group(5) or 0)
    ss = int(match.group(6) or 0)
    return date(y, m, d), (time(hh, mm, ss) if match.group(4) else None)


def extract_amount(cells: list[str], text: str) -> float | None:
    if is_table_transaction_row(cells):
        value = parse_float_cell(table_amount_cell(cells))
        if value is not None:
            return value
    candidates = []
    for cell in cells:
        if any(word in str(cell) for word in ("份", "份额")):
            continue
        if any(word in str(cell) for word in ("金额", "元", "￥", "¥")):
            value = parse_float(cell)
            if value is not None:
                candidates.append(value)
    if candidates:
        return max(candidates)
    if "元" in text or "金额" in text:
        values = [parse_float(item) for item in re.findall(r"[¥￥]?\d[\d,]*(?:\.\d+)?\s*元?", text)]
        values = [v for v in values if v is not None]
        return max(values) if values else None
    return None


def extract_share(cells: list[str], text: str) -> float | None:
    if is_table_transaction_row(cells):
        return parse_float_cell(table_share_cell(cells))
    for cell in cells:
        cell_text = str(cell)
        if "份" in cell_text or "份额" in cell_text:
            value = parse_float(cell_text)
            if value is not None:
                return value
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*份", text)
    return parse_float(match.group(1)) if match else None


def extract_fund_name(session: Session, cells: list[str], text: str) -> str:
    if is_table_transaction_row(cells):
        return normalize_fund_name(normalized_cell(cells[0]))
    aliases = session.exec(select(FundAlias)).all()
    for alias in aliases:
        if alias.keyword and alias.keyword in text:
            return normalize_fund_name(alias.keyword)
    best = ""
    for cell in cells:
        cleaned = str(cell).strip()
        if not cleaned or len(cleaned) < 3:
            continue
        if any(token in cleaned for token in ("基金", "混合", "股票", "债券", "指数", "ETF", "QDII", "货币", "LOF")):
            if len(cleaned) > len(best):
                best = cleaned
    return normalize_fund_name(best)


def match_alias(session: Session, fund_name: str) -> FundAlias | None:
    if not fund_name:
        return None
    normalized_name = normalize_fund_name_for_match(fund_name)
    aliases = session.exec(select(FundAlias)).all()
    for alias in aliases:
        if normalized_name in {normalize_fund_name_for_match(alias.keyword), normalize_fund_name_for_match(alias.fund_name)}:
            return alias
    for alias in aliases:
        alias_key = normalize_fund_name_for_match(alias.keyword)
        alias_name = normalize_fund_name_for_match(alias.fund_name)
        if alias_key and (alias_key in normalized_name or normalized_name in alias_key):
            return alias
        if alias_name and (alias_name in normalized_name or normalized_name in alias_name):
            return alias
    found = search_fund_safely(normalize_fund_name(fund_name))
    if found:
        alias = upsert_alias(session, fund_name, found["fund_code"], found.get("fund_name", fund_name), infer_fund_type(found.get("fund_name", fund_name), found["fund_code"]), "akshare")
        ensure_rule(session, alias.fund_code, alias.fund_name, alias.fund_type)
        return alias
    return None


def normalize_fund_name(name: str | None) -> str:
    value = str(name or "").strip()
    if not value:
        return ""
    value = value.replace("（", "(").replace("）", ")")
    value = re.sub(r"\s+", "", value)
    replacements = {
        "人民幣": "人民币",
        "人民市": "人民币",
        "人民 币": "人民币",
        "QDII )": "QDII)",
        "( QDII": "(QDII",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return value


def normalize_fund_name_for_match(name: str | None) -> str:
    value = normalize_fund_name(name).upper()
    value = value.replace("人民币份额", "")
    value = value.replace("人民币", "")
    value = value.replace("份额", "")
    return re.sub(r"[^0-9A-Z\u4e00-\u9fff]", "", value)


def search_fund_safely(name: str) -> dict | None:
    try:
        result = search_fund_by_name(name)
        if result and result.get("fund_code"):
            return result
    except Exception:
        return None
    return None


def seed_aliases_from_fund_map(session: Session) -> None:
    if not FUND_MAP_PATH.exists():
        return
    try:
        import pandas as pd
        rows = pd.read_excel(str(FUND_MAP_PATH), dtype=str).fillna("").to_dict("records")
    except Exception:
        return
    changed = False
    for row in rows:
        keyword = str(row.get("keyword") or row.get("fund_name") or "").strip()
        code = str(row.get("fund_code") or "").strip().zfill(6)
        if not keyword or not code or code == "000000":
            continue
        name = str(row.get("fund_name") or keyword).strip()
        fund_type = FundType(row.get("fund_type")) if row.get("fund_type") in FundType._value2member_map_ else infer_fund_type(name, code)
        if not session.exec(select(FundAlias).where(FundAlias.keyword == keyword, FundAlias.fund_code == code)).first():
            session.add(FundAlias(keyword=keyword, fund_code=code, fund_name=name, fund_type=fund_type, source="fund_map"))
            changed = True
        ensure_rule(session, code, name, fund_type)
    if changed:
        session.commit()


def upsert_alias(session: Session, keyword: str, fund_code: str, fund_name: str, fund_type: FundType, source: str) -> FundAlias:
    code = fund_code.strip().zfill(6)
    alias = session.exec(select(FundAlias).where(FundAlias.keyword == keyword.strip(), FundAlias.fund_code == code)).first()
    if alias:
        alias.fund_name = fund_name
        alias.fund_type = fund_type
        session.add(alias)
        return alias
    alias = FundAlias(keyword=keyword.strip(), fund_code=code, fund_name=fund_name, fund_type=fund_type, source=source)
    session.add(alias)
    return alias


def ensure_rule(session: Session, fund_code: str, fund_name: str = "", fund_type: FundType = FundType.unknown, sync: bool = False) -> FundRule:
    code = fund_code.strip().zfill(6)
    rule = session.get(FundRule, code)
    if not rule:
        rule = FundRule(fund_code=code, fund_name=fund_name, fund_type=fund_type)
        session.add(rule)
    else:
        if fund_name and not rule.fund_name:
            rule.fund_name = fund_name
        if fund_type != FundType.unknown and rule.fund_type == FundType.unknown:
            rule.fund_type = fund_type
        session.add(rule)
    if sync:
        try:
            synced = fetch_fund_rule_from_akshare(code)
        except Exception:
            return rule
        rule.fund_name = synced.fund_name or fund_name or rule.fund_name
        rule.fund_type = infer_fund_type(synced.fund_name or fund_name or rule.fund_name, code) if synced.fund_type else rule.fund_type
        rule.buy_confirm_days = synced.buy_confirm_days if synced.buy_confirm_days is not None else rule.buy_confirm_days
        rule.sell_confirm_days = synced.sell_confirm_days if synced.sell_confirm_days is not None else rule.sell_confirm_days
        rule.buy_fee_rate = synced.buy_fee_rate if synced.buy_fee_rate is not None else rule.buy_fee_rate
        rule.sync_source = synced.source
        rule.synced_at = now_shanghai_naive()
        session.add(rule)
        session.flush()
        for tier in synced.fee_tiers or []:
            exists = session.exec(
                select(FundFeeTier).where(
                    FundFeeTier.fund_code == code,
                    FundFeeTier.min_holding_days == tier[0],
                    FundFeeTier.max_holding_days == tier[1],
                    FundFeeTier.redemption_fee_rate == tier[2],
                )
            ).first()
            if not exists:
                session.add(FundFeeTier(fund_code=code, min_holding_days=tier[0], max_holding_days=tier[1], redemption_fee_rate=tier[2]))
    return rule


def ensure_sell_fee_rule(
    session: Session,
    candidate: TransactionCandidate,
    rule: FundRule,
    rule_sync_attempts: set[str] | None = None,
) -> FundRule:
    if candidate.action != TransactionAction.sell or not candidate.fund_code:
        return rule
    code = candidate.fund_code.zfill(6)
    existing_tier = session.exec(select(FundFeeTier).where(FundFeeTier.fund_code == code)).first()
    if existing_tier:
        return rule
    attempts = rule_sync_attempts if rule_sync_attempts is not None else set()
    if code in attempts:
        return rule
    attempts.add(code)
    return ensure_rule(session, code, candidate.fund_name, candidate.fund_type, sync=True)


def has_sell_fee_tiers(session: Session, fund_code: str) -> bool:
    return bool(session.exec(select(FundFeeTier).where(FundFeeTier.fund_code == fund_code.zfill(6))).first())


def infer_fund_type(fund_name: str | None, fund_code: str | None) -> FundType:
    name = (fund_name or "").upper().replace("（", "(").replace("）", ")")
    code = (fund_code or "").strip().zfill(6)
    if any(word in name for word in ("货币", "现金", "余额宝", "零钱", "添利", "天天利", "活期宝")):
        return FundType.money_fund
    if "ETF联接" in name or "ETF连接" in name or "LOF" in name:
        return FundType.open_fund
    if "ETF" in name or code.startswith(("510", "511", "512", "513", "515", "516", "517", "518", "560", "561", "562", "563", "588", "589", "159")):
        return FundType.etf
    if code and code != "000000":
        return FundType.open_fund
    return FundType.unknown


def effective_nav(session: Session, fund_code: str, trade_date: date, submitted_at: time | None, rule: FundRule) -> FundNav | None:
    target = effective_nav_target_date(trade_date, submitted_at, rule)
    return session.exec(select(FundNav).where(FundNav.fund_code == fund_code, FundNav.nav_date >= target).order_by(FundNav.nav_date)).first()


def effective_nav_with_auto_sync(
    session: Session,
    candidate: TransactionCandidate,
    rule: FundRule,
    nav_sync_attempts: set[str] | None = None,
) -> tuple[FundNav | None, str]:
    if not candidate.fund_code or not candidate.trade_date:
        return None, ""
    nav = effective_nav(session, candidate.fund_code, candidate.trade_date, candidate.submitted_at, rule)
    if nav:
        return nav, ""
    attempts = nav_sync_attempts if nav_sync_attempts is not None else set()
    code = candidate.fund_code.zfill(6)
    if code not in attempts:
        attempts.add(code)
        _, error = sync_nav_for_fund(session, code)
        nav = effective_nav(session, code, candidate.trade_date, candidate.submitted_at, rule)
        if nav:
            return nav, ""
        if error:
            return None, error
    target = effective_nav_target_date(candidate.trade_date, candidate.submitted_at, rule)
    return None, f"已同步但没有覆盖 {target} 之后的净值"


def effective_nav_target_date(trade_date: date, submitted_at: time | None, rule: FundRule) -> date:
    # OCR timestamps from domestic fund platforms are interpreted as Asia/Shanghai wall time.
    cutoff = parse_time(rule.cutoff_time) or time(15, 0)
    return trade_date + timedelta(days=1) if submitted_at and submitted_at >= cutoff else trade_date


def nth_nav_date(session: Session, fund_code: str, start: date, n: int) -> date:
    if n <= 0:
        return start
    navs = session.exec(select(FundNav).where(FundNav.fund_code == fund_code, FundNav.nav_date > start).order_by(FundNav.nav_date)).all()
    return navs[n - 1].nav_date if len(navs) >= n else start + timedelta(days=n)


def estimate_sell_fee(session: Session, candidate: TransactionCandidate, gross_amount: float | None = None) -> float:
    if candidate.action != TransactionAction.sell or not candidate.share:
        return 0.0
    gross = gross_amount if gross_amount is not None else (round(candidate.share * candidate.nav, 2) if candidate.nav else candidate.amount_cny)
    if not gross:
        return 0.0
    tiers = session.exec(select(FundFeeTier).where(FundFeeTier.fund_code == candidate.fund_code).order_by(FundFeeTier.min_holding_days)).all()
    if not tiers:
        return 0.0
    lots = fifo_lots_before(session, candidate)
    remaining = candidate.share
    fee = 0.0
    sell_date = candidate.confirm_date or candidate.effective_nav_date or candidate.trade_date
    for lot_date, lot_share in lots:
        if remaining <= 0:
            break
        used = min(remaining, lot_share)
        holding_days = (sell_date - lot_date).days if sell_date else 0
        rate = redemption_rate_for_days(tiers, holding_days)
        fee += (gross * (used / candidate.share)) * rate
        remaining -= used
    return round(fee, 2)


def fifo_lots_before(session: Session, candidate: TransactionCandidate) -> list[tuple[date, float]]:
    if not candidate.trade_date:
        return []
    lots: list[tuple[date, float]] = []
    entries: list[tuple[date, time, int, TransactionAction, float]] = []
    txs = session.exec(select(FundTransaction).where(FundTransaction.fund_code == candidate.fund_code, FundTransaction.trade_date <= candidate.trade_date).order_by(FundTransaction.trade_date, FundTransaction.id)).all()
    for tx in txs:
        if tx.action not in {TransactionAction.buy, TransactionAction.sell, TransactionAction.dividend_reinvest}:
            continue
        if tx.trade_date == candidate.trade_date and (tx.submitted_at or time.min) >= (candidate.submitted_at or time.max):
            continue
        lot_date = tx.confirm_date or tx.effective_nav_date or tx.trade_date
        entries.append((lot_date, tx.submitted_at or time.min, tx.id or 0, tx.action, tx.share or 0))
    candidate_query = (
        select(TransactionCandidate)
        .where(
            TransactionCandidate.fund_code == candidate.fund_code,
            TransactionCandidate.action.in_([TransactionAction.buy, TransactionAction.sell, TransactionAction.dividend_reinvest]),
            TransactionCandidate.trade_date <= candidate.trade_date,
            TransactionCandidate.posted_transaction_id.is_(None),
        )
        .order_by(TransactionCandidate.trade_date, TransactionCandidate.submitted_at, TransactionCandidate.id)
    )
    for row in session.exec(candidate_query).all():
        if row.id == candidate.id:
            continue
        if row.trade_date == candidate.trade_date:
            if (row.submitted_at or time.min) > (candidate.submitted_at or time.max):
                continue
            if (row.submitted_at or time.min) == (candidate.submitted_at or time.max) and (row.id or 0) >= (candidate.id or 0):
                continue
        lot_date = row.confirm_date or row.effective_nav_date or row.trade_date
        if lot_date:
            entries.append((lot_date, row.submitted_at or time.min, row.id or 0, row.action, row.share or 0))
    for lot_date, _, _, action, share in sorted(entries, key=lambda item: (item[0], item[1], item[2])):
        if action in {TransactionAction.buy, TransactionAction.dividend_reinvest}:
            lots.append((lot_date, share))
        elif action == TransactionAction.sell:
            remaining = share
            new_lots = []
            for lot_date, lot_share in lots:
                if remaining <= 0:
                    new_lots.append((lot_date, lot_share))
                    continue
                used = min(remaining, lot_share)
                remaining -= used
                if lot_share > used:
                    new_lots.append((lot_date, lot_share - used))
            lots = new_lots
    return lots


def redemption_rate_for_days(tiers: list[FundFeeTier], days: int) -> float:
    for tier in tiers:
        if days >= tier.min_holding_days and (tier.max_holding_days is None or days < tier.max_holding_days):
            return tier.redemption_fee_rate
    return 0.0


def calculate_positions(session: Session, include_closed: bool = True) -> list[dict]:
    txs = session.exec(select(FundTransaction).order_by(FundTransaction.trade_date, FundTransaction.id)).all()
    money_or_etf = {r.fund_code for r in session.exec(select(FundRule)).all() if r.fund_type in {FundType.etf, FundType.money_fund}}
    grouped: dict[str, dict] = {}
    for tx in txs:
        if tx.fund_code in money_or_etf:
            continue
        item = grouped.setdefault(tx.fund_code, {"fund_code": tx.fund_code, "fund_name": tx.fund_name, "share": 0.0, "cost": 0.0, "realized_profit": 0.0, "last_trade_date": tx.trade_date})
        item["fund_name"] = tx.fund_name or item["fund_name"]
        item["last_trade_date"] = tx.trade_date
        amount = tx.amount_cny or 0.0
        fee = tx.fee or 0.0
        if tx.action == TransactionAction.buy:
            item["share"] += tx.share or ((amount - fee) / tx.nav if tx.nav else 0)
            item["cost"] += amount + fee
        elif tx.action == TransactionAction.sell:
            old_share = item["share"]
            sell_share = tx.share or 0
            cost_reduction = item["cost"] * min(sell_share / old_share, 1.0) if old_share else 0
            item["cost"] -= cost_reduction
            item["share"] = max(item["share"] - sell_share, 0)
            item["realized_profit"] += max(amount, 0) - cost_reduction
            if item["share"] < 0.0001:
                item["share"] = 0
                item["cost"] = 0
        elif tx.action == TransactionAction.dividend:
            item["realized_profit"] += amount
        elif tx.action == TransactionAction.dividend_reinvest:
            item["share"] += tx.share or 0
    latest = latest_navs(session, set(grouped))
    positions = []
    for code, item in grouped.items():
        nav = latest.get(code)
        latest_nav = nav.unit_nav if nav else None
        market = item["share"] * latest_nav if latest_nav else 0.0
        profit = market - item["cost"] if latest_nav else 0.0
        is_closed = item["share"] < 0.0001
        if is_closed and not include_closed:
            continue
        positions.append({**item, "latest_nav": latest_nav, "nav_date": nav.nav_date if nav else None, "market_value": market, "profit": profit, "profit_rate": profit / item["cost"] if item["cost"] else None, "is_closed": is_closed})
    return sorted(positions, key=lambda p: (p["is_closed"], -p["market_value"], p["fund_code"]))


def latest_navs(session: Session, codes: set[str]) -> dict[str, FundNav]:
    result = {}
    for code in codes:
        nav = session.exec(select(FundNav).where(FundNav.fund_code == code).order_by(desc(FundNav.nav_date))).first()
        if nav:
            result[code] = nav
    return result


def build_fund_chart(txs: list[FundTransaction], navs: list[FundNav]) -> dict:
    if not navs:
        return {"points": [], "markers": []}
    base = navs[0].unit_nav
    points = [{"date": str(nav.nav_date), "value": nav.unit_nav / base - 1} for nav in navs if base]
    markers = []
    for tx in txs:
        if tx.action not in {TransactionAction.buy, TransactionAction.sell, TransactionAction.dividend_reinvest}:
            continue
        nav = next((n for n in navs if n.nav_date >= tx.trade_date), None)
        if nav:
            markers.append({"date": str(nav.nav_date), "action": tx.action.value, "value": nav.unit_nav / base - 1})
    return {"points": points, "markers": markers}


def apply_candidate_form(candidate: TransactionCandidate, fund_code: str, fund_name: str, fund_type: str, action: str, row_status: str, trade_date: str, submitted_at: str, amount_cny: str, share: str, nav: str, fee: str) -> None:
    candidate.fund_code = fund_code.strip().zfill(6) if fund_code.strip() else ""
    candidate.fund_name = fund_name.strip()
    candidate.fund_type = FundType(fund_type) if fund_type in FundType._value2member_map_ else FundType.unknown
    candidate.action = TransactionAction(action) if action in TransactionAction._value2member_map_ else None
    candidate.row_status = RowStatus(row_status) if row_status in RowStatus._value2member_map_ else RowStatus.unknown
    candidate.trade_date = date.fromisoformat(trade_date) if trade_date else None
    candidate.submitted_at = parse_time(submitted_at)
    candidate.amount_cny = parse_float(amount_cny)
    candidate.share = parse_float(share)
    candidate.nav = parse_float(nav)
    candidate.fee = parse_float(fee)
    if candidate.row_status == RowStatus.success and candidate.event_type == EventType.ignored_status:
        candidate.event_type = None
    if candidate.action is not None and candidate.event_type == EventType.other:
        candidate.event_type = None
    candidate.updated_at = now_shanghai_naive()


def parse_time(value: str | None) -> time | None:
    if not value:
        return None
    match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", value)
    if not match:
        return None
    return time(int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def parse_float(value) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).replace(",", "").replace("¥", "").replace("￥", "").replace("元", "").replace("份", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def preserve_masked_secret(submitted: str, current: str) -> str:
    value = submitted.strip()
    if not value:
        return current
    if set(value) <= {"*"} or value == "未配置" or value.startswith("已配置"):
        return current
    return value


def _hash_content(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

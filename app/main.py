import json
import os
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, desc, select
from starlette.status import HTTP_303_SEE_OTHER

from .app_settings import configured, masked, runtime_settings, save_settings
from .auth import add_session_middleware, current_user, login_user, logout_user, verify_login
from .config import ensure_data_dirs, settings
from .db import engine, get_session, init_db
from .models import AppSetting, ImportDocument, ImportStatus
from .ocr import recognize_file
from .templates import templates


def _hash_content(content: bytes) -> str:
    import hashlib
    return hashlib.sha256(content).hexdigest()


def _hash_file(path: Path) -> str:
    return _hash_content(path.read_bytes())


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


@app.exception_handler(401)
def auth_exception(request: Request, exc: HTTPException) -> HTMLResponse:
    return templates.TemplateResponse("login.html", {"request": request, "next": request.url.path}, status_code=401)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
def login_submit(username: str = Form(""), password: str = Form(""), next: str = Form("")):
    user = verify_login(username, password)
    if user:
        resp = redirect(next or "/")
        login_user(resp, user)
        return resp
    return templates.TemplateResponse("login.html", {"request": Request, "error": "用户名或密码错误"}, status_code=401)


@app.post("/logout")
def logout():
    resp = redirect("/login")
    logout_user(resp)
    return resp


@app.get("/", response_class=HTMLResponse)
def root(_: str = Depends(require_user)):
    return redirect("/imports")


@app.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, _: str = Depends(require_user)):
    return templates.TemplateResponse("upload.html", {"request": request})


@app.post("/upload")
def upload_submit(
    request: Request,
    raw_text: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    file: UploadFile | None = File(None),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    ensure_data_dirs()
    uploaded_files = [item for item in files if item and item.filename]
    if file and file.filename:
        uploaded_files.append(file)

    documents: list[ImportDocument] = []
    for upload in uploaded_files:
        existing = session.exec(
            select(ImportDocument).where(
                ImportDocument.file_name == upload.filename,
                ImportDocument.status != ImportStatus.error,
            )
        ).first()
        if existing:
            return redirect(f"/imports/{existing.id}?message=文件「{upload.filename}」已存在（导入 #{existing.id}）")
        content = upload.file.read()
        safe_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}-{Path(upload.filename).name}"
        target = settings.uploads_dir / safe_name
        target.write_bytes(content)
        source_hash = _hash_file(target)
        doc = ImportDocument(
            file_name=upload.filename,
            source_file=str(target),
            source_hash=source_hash,
            content_type=upload.content_type,
            status=ImportStatus.uploaded,
        )
        session.add(doc)
        documents.append(doc)

    if raw_text.strip():
        source_hash = _hash_content(raw_text.encode())
        doc = ImportDocument(
            raw_text=raw_text,
            source_hash=source_hash,
            status=ImportStatus.uploaded,
        )
        session.add(doc)
        documents.append(doc)

    session.commit()
    for doc in documents:
        session.refresh(doc)

    if len(documents) == 1:
        return redirect(f"/imports/{documents[0].id}")
    return redirect("/imports")


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
        query = query.where(ImportDocument.status != ImportStatus.error)
    elif show == "all":
        pass
    documents = session.exec(query).all()
    total = len(documents)
    ocr_done = sum(1 for d in documents if d.status == ImportStatus.ocr_done)
    pending = sum(1 for d in documents if d.status == ImportStatus.uploaded)
    failed = sum(1 for d in documents if d.status == ImportStatus.error)
    return templates.TemplateResponse("imports.html", {
        "request": request, "documents": documents,
        "total": total, "ocr_done": ocr_done, "pending": pending, "failed": failed,
        "show": show, "message": message,
    })


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
    return templates.TemplateResponse("import_detail.html", {
        "request": request, "document": document,
        "message": message,
        "ocr_backend": config.get("OCR_BACKEND", "rapidocr"),
    })


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
    try:
        result = recognize_file(document.source_file, runtime_settings(session))
    except Exception as exc:
        document.status = ImportStatus.error
        document.error_message = str(exc)
        document.updated_at = datetime.utcnow()
        session.add(document)
        session.commit()
        return redirect(f"/imports/{document_id}?message=OCR 失败: {exc}")
    document.ocr_text = result.text
    if result.rows:
        grid_path = _grid_path(document.source_file)
        try:
            with open(grid_path, "w") as f:
                json.dump(result.rows, f, ensure_ascii=False)
        except Exception:
            pass
    document.status = ImportStatus.ocr_done
    document.error_message = ""
    document.updated_at = datetime.utcnow()
    session.add(document)
    session.commit()
    return redirect(f"/imports/{document_id}?message=OCR 完成，识别 {len(result.text)} 字")


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
        if path.exists():
            path.unlink()
        grid_path = _grid_path(str(path))
        if grid_path.exists():
            grid_path.unlink()
    session.delete(document)
    session.commit()
    return redirect("/imports?message=已删除")


from concurrent.futures import ThreadPoolExecutor


def _grid_path(source_file: str) -> Path:
    p = settings.uploads_dir / "parsed" / (Path(source_file).name + ".grid.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _txt_path(source_file: str) -> Path:
    p = settings.uploads_dir / "text" / (Path(source_file).name + ".txt")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
_OCR_EXECUTOR = ThreadPoolExecutor(max_workers=10, thread_name_prefix="fund-ocr")


def _run_one_ocr(document_id: int):
    from .models import ImportStatus
    from .db import engine as _eng
    from sqlmodel import Session as _Session
    with _Session(_eng) as session:
        doc = session.get(ImportDocument, document_id)
        if not doc or not doc.source_file:
            return
        doc.status = ImportStatus.ocr_running
        doc.updated_at = datetime.utcnow()
        session.add(doc)
        session.commit()
        try:
            import json as _json
            config = runtime_settings(session)
            result = recognize_file(doc.source_file, config)
            doc.ocr_text = result.text
            if result.rows:
                grid_path = _grid_path(doc.source_file)
                with open(grid_path, "w") as f:
                    _json.dump(result.rows, f, ensure_ascii=False)
            doc.status = ImportStatus.ocr_done
        except Exception as e:
            doc.status = ImportStatus.error
            doc.error_message = str(e)
        doc.updated_at = datetime.utcnow()
        session.add(doc)
        session.commit()


@app.post("/imports/ocr-all")
def imports_ocr_all(
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    from .models import ImportStatus
    documents = session.exec(
        select(ImportDocument)
        .where(ImportDocument.status == ImportStatus.uploaded, ImportDocument.source_file.isnot(None))
        .order_by(ImportDocument.created_at)
    ).all()
    if not documents:
        return redirect("/imports?message=没有待 OCR 的导入")
    for doc in documents:
        _OCR_EXECUTOR.submit(_run_one_ocr, doc.id)
    return redirect(f"/imports?message=已加入 {len(documents)} 个 OCR 任务")


@app.post("/imports/{document_id}/archive")
def import_archive(
    document_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    document = session.get(ImportDocument, document_id)
    if not document:
        raise HTTPException(status_code=404)
    document.status = ImportStatus.error
    document.updated_at = datetime.utcnow()
    session.add(document)
    session.commit()
    return redirect("/imports")


@app.post("/imports/save-all-ocr")
def imports_save_all_ocr(
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    from .models import ImportStatus
    docs = session.exec(select(ImportDocument).where(ImportDocument.status == ImportStatus.ocr_done)).all()
    if not docs:
        return redirect("/imports?message=没有已完成的 OCR 结果")
    count = 0
    for doc in docs:
        if doc.source_file and doc.ocr_text:
            txt_path = _txt_path(doc.source_file)
            Path(txt_path).write_text(doc.ocr_text, encoding="utf-8")
            count += 1
    return redirect(f"/imports?message=已保存 {count} 个 OCR 结果到 uploads/ 目录")


@app.post("/imports/delete-all")
def imports_delete_all(
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    import os as _os, json as _json
    from .models import ImportStatus
    docs = session.exec(select(ImportDocument)).all()
    for doc in docs:
        if doc.source_file:
            p = Path(doc.source_file)
            if p.exists(): p.unlink()
            gp = _grid_path(str(p))
            if gp.exists(): gp.unlink()
        session.delete(doc)
    session.commit()
    return redirect("/imports?message=已删除全部导入")


@app.post("/imports/{document_id}/save-ocr")
def import_save_ocr(
    document_id: int,
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    doc = session.get(ImportDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404)
    if not doc.ocr_text and not doc.source_file:
        return redirect(f"/imports/{document_id}?message=没有可保存的 OCR 结果")
    if doc.source_file and doc.ocr_text:
        txt_path = _txt_path(doc.source_file)
        Path(txt_path).write_text(doc.ocr_text, encoding="utf-8")
    return redirect(f"/imports/{document_id}?message=OCR 结果已保存")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    message: str = "",
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    config = runtime_settings(session)
    return templates.TemplateResponse("settings.html", {
        "request": request, "config": config, "message": message,
        "masked": masked, "configured": configured,
    })


@app.post("/settings")
def settings_update(
    ocr_enabled: str = Form("off"),
    ocr_backend: str = Form("rapidocr"),
    baidu_ocr_api_key: str = Form(""),
    baidu_ocr_secret_key: str = Form(""),
    _: str = Depends(require_user),
    session: Session = Depends(get_session),
):
    current = runtime_settings(session)
    values = {
        "OCR_ENABLED": "true" if ocr_enabled == "on" else "false",
        "OCR_BACKEND": ocr_backend,
        "BAIDU_OCR_API_KEY": baidu_ocr_api_key.strip() or current.get("BAIDU_OCR_API_KEY", ""),
        "BAIDU_OCR_SECRET_KEY": baidu_ocr_secret_key.strip() or current.get("BAIDU_OCR_SECRET_KEY", ""),
    }
    save_settings(session, values)
    return redirect("/settings?message=设置已保存")


# ── 流水表 ──────────────────────────────────────────────────

LEDGER_PATH = settings.data_dir / "ledger_result.json"
EXTRACTING_FLAG = settings.data_dir / ".extracting"
FUND_MAP_PATH = Path(__file__).parent.parent / "fund_map.xlsx"


def infer_fund_type(fund_name: str | None, fund_code: str | None) -> str:
    name = (fund_name or "").upper().replace("（", "(").replace("）", ")")
    code = str(fund_code or "").strip().zfill(6)

    if not name and not code:
        return "unknown"

    if "ETF联接" in name or "ETF连接" in name:
        return "open_fund"

    if "LOF" in name:
        return "open_fund"

    for kw in ("货币", "现金", "现金宝", "余额宝", "零钱", "添利", "天天利", "活期宝", "收益宝", "理财宝"):
        if kw in name:
            return "money_fund"

    if "ETF" in name or "交易型开放式指数基金" in name:
        return "etf"

    if code != "000000":
        for prefix in ("510", "511", "512", "513", "515", "516", "517", "518",
                       "560", "561", "562", "563", "588", "589", "159", "16", "18"):
            if code.startswith(prefix):
                return "etf"
        for prefix in ("00", "01", "02", "03", "04", "05", "06", "07", "08", "09",
                       "10", "11", "12", "13", "14"):
            if code.startswith(prefix):
                return "open_fund"

    return "open_fund"


def _load_ledger() -> list[dict]:
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text())
        except Exception:
            pass
    return []


def _save_ledger(rows: list[dict]) -> None:
    LEDGER_PATH.write_text(json.dumps(rows, ensure_ascii=False, default=str))


def _load_fund_map() -> list[dict]:
    if FUND_MAP_PATH.exists():
        try:
            import pandas as pd
            df = pd.read_excel(str(FUND_MAP_PATH), dtype=str)
            return df.fillna("").to_dict("records")
        except Exception:
            pass
    return []


def _clean_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("￥", "").replace("¥", "").replace("元", "").strip()
    if s in ("", "--", "-", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _std_status(raw: str) -> str:
    if not raw:
        return "未知"
    for kws, s in [(("成功", "已确认", "确认成功", "已完成"), "成功"),
                    (("失败",), "失败"),
                    (("撤回", "撤销", "已撤销", "取消", "已取消", "撤单"), "已撤销")]:
        for kw in kws:
            if kw in raw:
                return s
    return "未知"


def _std_action(raw: str) -> tuple[str, str]:
    m = {"buy": ("buy", "买入"), "申购": ("buy", "买入"), "买入": ("buy", "买入"),
         "sell": ("sell", "卖出"), "赎回": ("sell", "卖出"), "卖出": ("sell", "卖出"),
         "dividend": ("dividend", "分红"), "分红": ("dividend", "分红"),
         "dividend_reinvest": ("dividend_reinvest", "红利再投资"), "红利再投": ("dividend_reinvest", "红利再投资")}
    if not raw:
        return ("", "")
    rl = raw.lower().strip()
    if rl in m:
        return m[rl]
    for k in m:
        if k in raw:
            return m[k]
    return (raw, raw)


def _match_fund(name: str, fund_map: list[dict]) -> dict:
    if not name or not fund_map:
        return {}
    for e in fund_map:
        kw = e.get("keyword", "")
        if kw and kw in name:
            return {"fund_name": e.get("fund_name", name), "fund_code": e.get("fund_code", ""), "fund_type": e.get("fund_type", "unknown")}
    return {}


@app.get("/ledger", response_class=HTMLResponse)
def ledger_page(
    request: Request,
    message: str = "",
    _: str = Depends(require_user),
):
    extracting = EXTRACTING_FLAG.exists()
    rows = _load_ledger()
    total = len(rows)
    success = sum(1 for r in rows if r.get("status_std") == "成功")
    non_success = total - success
    unmatched = sum(1 for r in rows if r.get("fund_code") == "" and r.get("fund_name_raw"))
    return templates.TemplateResponse("ledger.html", {
        "request": request, "rows": rows, "total": total,
        "success": success, "non_success": non_success, "unmatched": unmatched,
        "extracting": extracting, "message": message,
    })


@app.post("/ledger/clean")
def ledger_clean(
    _: str = Depends(require_user),
):
    import re as _re
    parsed_dir = settings.uploads_dir / "parsed"
    if not parsed_dir.exists():
        return redirect("/ledger?message=没有找到 OCR 数据")
    fund_map = _load_fund_map()

    records = []
    for fname in sorted(os.listdir(str(parsed_dir))):
        if not fname.endswith(".grid.json"):
            continue
        path = parsed_dir / fname
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for r in items:
            r["_source_file"] = fname
            records.append(r)

    if not records:
        return redirect("/ledger?message=没有找到 OCR 数据")

    rows = []
    for r in records:
        fn_raw = _re.sub(r"\s+", "", (r.get("fund_name") or "").replace("\n", "").replace("\r", ""))
        a_std, a_dir = _std_action(r.get("action", ""))
        s_std = _std_status(r.get("status", ""))
        matched = _match_fund(fn_raw, fund_map)
        note = ""
        if not matched:
            note = "未匹配基金"
        ft = matched.get("fund_type", "") if matched else ""
        if not ft:
            ft = infer_fund_type(fn_raw, matched.get("fund_code", "") if matched else "")
        rows.append({
            "source_file": r.get("_source_file", ""),
            "fund_name_raw": fn_raw,
            "fund_name": matched.get("fund_name", fn_raw) if matched else fn_raw,
            "fund_code": matched.get("fund_code", "") if matched else "",
            "fund_type": ft,
            "action": a_std,
            "direction": a_dir,
            "trade_date": r.get("trade_date", ""),
            "submitted_at": r.get("submitted_at", ""),
            "share": _clean_num(r.get("share")),
            "amount_cny": _clean_num(r.get("amount_cny")),
            "status": r.get("status", ""),
            "status_std": s_std,
            "nav": None,
            "nav_date": None,
            "note": note,
            "estimating": False,
        })

    _save_ledger(rows)
    return redirect(f"/ledger?message=清洗完成: 共 {len(rows)} 条, 未匹配基金 {sum(1 for r in rows if not r['fund_code'])} 条")


@app.post("/ledger/fetch-nav")
def ledger_fetch_nav(
    _: str = Depends(require_user),
):
    rows = _load_ledger()
    if not rows:
        return redirect("/ledger?message=请先执行清洗")

    try:
        import akshare as ak
        import pandas as pd
    except ImportError:
        return redirect("/ledger?message=需要 akshare: pip install akshare")

    success_rows = [r for r in rows if r["status_std"] == "成功" and r.get("fund_code")]
    if not success_rows:
        return redirect("/ledger?message=没有需要查询净值的成功流水（先配 fund_map.xlsx）")

    by_code: dict[str, list[dict]] = {}
    for r in success_rows:
        by_code.setdefault(r["fund_code"], []).append(r)

    total_fetched = 0
    for code, items in by_code.items():
        fund_type = items[0].get("fund_type", "open_fund")
        trade_dates = [d for d in (r.get("trade_date") for r in items) if d]
        if not trade_dates:
            continue
        start = min(trade_dates)[:10]
        end = max(trade_dates)[:10]
        nav_map = {}
        try:
            if fund_type == "etf":
                df = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=start, end_date=end, adjust="")
                if "收盘" in df.columns:
                    for _, row in df.iterrows():
                        try:
                            d = pd.to_datetime(row["日期"]).date()
                            nav_map[d.isoformat()] = float(row["收盘"])
                        except Exception:
                            pass
            else:
                df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
                if "单位净值" in df.columns and "净值日期" in df.columns:
                    for _, row in df.iterrows():
                        try:
                            d = pd.to_datetime(row["净值日期"]).date()
                            nav_map[d.isoformat()] = float(row["单位净值"])
                        except Exception:
                            pass
        except Exception as e:
            for r in items:
                r["note"] = (r.get("note", "") + "；" if r.get("note") else "") + f"查询失败"
            continue

        total_fetched += len(items)
        for r in items:
            td = r.get("trade_date", "")[:10]
            if td in nav_map:
                r["nav"] = nav_map[td]
                r["nav_date"] = td
            else:
                dates = sorted(nav_map.keys())
                nearest = None
                for d in dates:
                    if d >= td:
                        nearest = d
                        break
                if nearest is None and dates:
                    nearest = dates[-1]
                if nearest:
                    r["nav"] = nav_map[nearest]
                    r["nav_date"] = nearest
                else:
                    r["note"] = (r.get("note", "") + "；" if r.get("note") else "") + "净值缺失"

    _save_ledger(rows)
    return redirect(f"/ledger?message=净值查询完成: 已处理 {total_fetched} 条")


def _extract_type(fund_type: str) -> str:
    EXTRACTING_FLAG.touch()
    try:
        return _do_extract(fund_type)
    finally:
        EXTRACTING_FLAG.unlink(missing_ok=True)


def _do_extract(fund_type: str) -> str:
    rows = _load_ledger()
    if not rows:
        return "请先执行清洗"
    target = [r for r in rows if r.get("fund_type") == fund_type and r["status_std"] == "成功"]
    if not target:
        return f"没有符合条件的 {fund_type} 记录"
    codes = set(r.get("fund_code") for r in target if r.get("fund_code"))
    if not codes:
        return "请先在 fund_map.xlsx 中填入基金代码后再试"

    try:
        import akshare as ak
        import pandas as pd
    except ImportError:
        return "需要 akshare"

    for code in sorted(codes):
        items = [r for r in target if r.get("fund_code") == code]
        trade_dates = sorted(set(r["trade_date"][:10] for r in items if r.get("trade_date")))
        if not trade_dates:
            continue
        start, end = trade_dates[0], trade_dates[-1]
        ft = items[0].get("fund_type", "open_fund")
        nav_map = {}
        try:
            if ft == "etf":
                df = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=start, end_date=end, adjust="")
                if "收盘" in df.columns:
                    for _, row in df.iterrows():
                        nav_map[pd.to_datetime(row["日期"]).date().isoformat()] = float(row["收盘"])
            else:
                df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
                if "单位净值" in df.columns and "净值日期" in df.columns:
                    for _, row in df.iterrows():
                        nav_map[pd.to_datetime(row["净值日期"]).date().isoformat()] = float(row["单位净值"])
        except Exception:
            for r in items:
                r["note"] = (r.get("note", "") + "；" if r.get("note") else "") + "净值查询失败"
            continue

        for r in items:
            td = r["trade_date"][:10]
            eff_date = td
            sa = r.get("submitted_at", "")
            if sa and len(sa) >= 16:
                try:
                    h, m = int(sa[11:13]), int(sa[14:16])
                    if h > 15 or (h == 15 and m > 0):
                        from datetime import date, timedelta
                        dt = pd.to_datetime(td).date()
                        for i in range(1, 10):
                            nd = (dt + timedelta(days=i)).isoformat()
                            if nd in nav_map:
                                eff_date = nd
                                break
                except Exception:
                    pass
            if eff_date in nav_map:
                r["nav"] = nav_map[eff_date]
                r["nav_date"] = eff_date
                action = r.get("action", "")
                amt = r.get("amount_cny")
                sh = r.get("share")
                nv = nav_map[eff_date]
                if action == "buy" and amt and not sh:
                    r["estimated_share"] = round(amt / nv, 2)
                elif action == "sell" and sh and not amt:
                    r["estimated_amount_cny"] = round(sh * nv, 2)
            else:
                r["note"] = (r.get("note", "") + "；" if r.get("note") else "") + "净值缺失"

    _save_ledger(rows)
    return f"{fund_type} 提取完成: 已处理 {len(target)} 条"


@app.post("/ledger/extract/{fund_type}")
def ledger_extract(
    fund_type: str,
    bg: BackgroundTasks,
    _: str = Depends(require_user),
):
    rows = _load_ledger()
    if not rows:
        return redirect("/ledger?message=请先执行清洗")
    target = [r for r in rows if r.get("fund_type") == fund_type and r["status_std"] == "成功"]
    if not target:
        return redirect(f"/ledger?message=没有符合条件的 {fund_type} 记录")
    codes = set(r.get("fund_code") for r in target if r.get("fund_code"))
    if not codes:
        return redirect(f"/ledger?message=请先在 fund_map.xlsx 中填入基金代码后再试")

    bg.add_task(_extract_type, fund_type)
    return redirect(f"/ledger?message={fund_type} 提取已开始，请稍后刷新查看结果")



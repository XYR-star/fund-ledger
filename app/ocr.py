from dataclasses import dataclass
import base64
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

import requests


@dataclass
class OcrResult:
    text: str
    confidence: float | None
    rows: list[dict] | None = None


_ENGINE = None
_BACKEND = None
_ENGINE_LOCK = Lock()


def recognize_file(path: str | Path, config: dict[str, str] | None = None) -> OcrResult:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(str(file_path))
    if _config_value(config, "OCR_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("OCR is disabled in settings")
    backend = _config_value(config, "OCR_BACKEND", "rapidocr").lower()
    if backend == "baidu_table":
        return _recognize_with_baidu_table(file_path, config or {})
    if backend == "api":
        return _recognize_with_api(file_path, config or {})
    if file_path.suffix.lower() == ".pdf":
        return _recognize_pdf(file_path, config)
    return _recognize_image(file_path, config)


def _get_engine(config: dict[str, str] | None = None):
    global _BACKEND, _ENGINE
    backend = _config_value(config, "OCR_BACKEND", "rapidocr").lower()
    if _ENGINE is not None and _BACKEND == backend:
        return _BACKEND, _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is not None and _BACKEND == backend:
            return _BACKEND, _ENGINE
        if backend == "paddle":
            _ENGINE = _get_paddle_engine()
            _BACKEND = "paddle"
        else:
            from rapidocr_onnxruntime import RapidOCR
            _ENGINE = RapidOCR()
            _BACKEND = "rapidocr"
    return _BACKEND, _ENGINE


def _get_paddle_engine():
    from paddleocr import PaddleOCR

    options = {
        "use_angle_cls": True,
        "lang": "ch",
        "use_gpu": False,
        "enable_mkldnn": False,
        "cpu_threads": 1,
    }
    try:
        return PaddleOCR(**options, show_log=False)
    except TypeError:
        return PaddleOCR(**options)


def _recognize_image(path: Path, config: dict[str, str] | None = None) -> OcrResult:
    backend, engine = _get_engine(config)
    if backend == "rapidocr":
        result, _ = engine(str(path))
    else:
        result = engine.ocr(str(path), cls=True)
    return _flatten_result(result)


def _recognize_pdf(path: Path, config: dict[str, str] | None = None) -> OcrResult:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF OCR requires PyMuPDF/fitz") from exc

    texts: list[str] = []
    confidences: list[float] = []
    with TemporaryDirectory() as tmpdir:
        doc = fitz.open(path)
        for page_index in range(len(doc)):
            page = doc.load_page(page_index)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image_path = Path(tmpdir) / f"page-{page_index + 1}.png"
            pix.save(image_path)
            item = _recognize_image(image_path, config)
            if item.text:
                texts.append(item.text)
            if item.confidence is not None:
                confidences.append(item.confidence)
    confidence = sum(confidences) / len(confidences) if confidences else None
    return OcrResult(text="\n".join(texts), confidence=confidence)


def _recognize_with_api(path: Path, config: dict[str, str]) -> OcrResult:
    url = _config_value(config, "OCR_API_URL")
    if not url:
        raise RuntimeError("OCR API URL is not configured")
    field = _config_value(config, "OCR_API_FILE_FIELD", "file")
    headers = {}
    api_key = _config_value(config, "OCR_API_KEY")
    if api_key:
        header = _config_value(config, "OCR_API_AUTH_HEADER", "Authorization")
        prefix = _config_value(config, "OCR_API_AUTH_PREFIX", "Bearer ")
        if prefix.lower() == "bearer":
            prefix = "Bearer "
        headers[header] = f"{prefix}{api_key}"
    with path.open("rb") as handle:
        response = requests.post(
            url,
            headers=headers,
            files={field: (path.name, handle)},
            timeout=90,
        )
    response.raise_for_status()
    data = response.json()
    text = _value_by_path(data, _config_value(config, "OCR_API_TEXT_PATH", "text"))
    if isinstance(text, list):
        text = "\n".join(str(item) for item in text)
    return OcrResult(text=str(text or ""), confidence=None)


def _recognize_with_baidu_table(path: Path, config: dict[str, str]) -> OcrResult:
    api_key = _config_value(config, "BAIDU_OCR_API_KEY")
    secret_key = _config_value(config, "BAIDU_OCR_SECRET_KEY")
    endpoint = _config_value(
        config,
        "BAIDU_TABLE_OCR_ENDPOINT",
        "https://aip.baidubce.com/rest/2.0/ocr/v1/table",
    )
    if not api_key or not secret_key:
        raise RuntimeError("Baidu OCR API key and secret key are not configured")

    token_response = requests.post(
        "https://aip.baidubce.com/oauth/2.0/token",
        params={
            "grant_type": "client_credentials",
            "client_id": api_key,
            "client_secret": secret_key,
        },
        timeout=30,
    )
    token_response.raise_for_status()
    access_token = token_response.json().get("access_token")
    if not access_token:
        raise RuntimeError("Baidu OCR access token response did not include access_token")

    response = requests.post(
        endpoint,
        params={"access_token": access_token},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "image": base64.b64encode(path.read_bytes()).decode("ascii"),
        },
        timeout=90,
    )
    response.raise_for_status()
    data = response.json()
    if "error_code" in data:
        raise RuntimeError(
            f"百度表格OCR API 返回错误: {data.get('error_code')} - {data.get('error_msg', '')}"
        )

    tables = data.get("tables_result") or []
    body: list[dict] = []
    for table in tables:
        if isinstance(table, dict):
            body.extend(table.get("body") or [])

    if body:
        all_rows = _cells_to_grid(body)
    else:
        html_data = data.get("result", {}).get("data") if isinstance(data.get("result"), dict) else None
        if isinstance(data.get("result"), list):
            for r in data["result"]:
                if isinstance(r, dict) and r.get("data"):
                    html_data = r["data"]
                    break
        all_rows = _parse_table_html(html_data) if html_data else []

    if not all_rows:
        import json as _json
        snippet = _json.dumps(data, ensure_ascii=False)[:500]
        raise RuntimeError(
            f"百度表格OCR 未识别到表格结构。API返回片段: {snippet}"
        )

    flat_text = "\n".join(" ".join(c for c in row if c) for row in all_rows)
    rows = _grid_to_transactions(all_rows)
    return OcrResult(text=flat_text, confidence=None, rows=rows)


def _cells_to_grid(body: list[dict]) -> list[list[str]]:
    max_row = 0
    max_col = 0
    for cell in body:
        r_end = cell.get("row_end", 0)
        c_end = cell.get("col_end", 0)
        max_row = max(max_row, r_end)
        max_col = max(max_col, c_end)

    grid = [["" for _ in range(max_col)] for _ in range(max_row)]

    for cell in body:
        text = (cell.get("words") or cell.get("cell") or "").strip()
        r0 = cell.get("row_start", 0)
        c0 = cell.get("col_start", 0)
        grid[r0][c0] = text

    return grid


def _parse_table_html(html: str) -> list[list[str]]:
    class _Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows: list[list[str]] = []
            self._row: list[str] = []
            self._capture = False
        def handle_starttag(self, tag, attrs):
            if tag == "td":
                self._capture = True
                self._row.append("")
        def handle_endtag(self, tag):
            if tag == "td":
                self._capture = False
            elif tag == "tr" and self._row:
                self.rows.append(self._row)
                self._row = []
        def handle_data(self, data):
            if self._capture and self._row:
                self._row[-1] += data.strip()

    parser = _Parser()
    parser.feed(html)
    if parser._row:
        parser.rows.append(parser._row)
    return parser.rows


def _grid_to_transactions(grid: list[list[str]]) -> list[dict]:
    if not grid or len(grid) < 2:
        return []

    col_types = _identify_column_types(grid)
    if not col_types:
        return []

    data_start = _find_data_start(grid, col_types)
    rows: list[dict] = []
    for row_idx in range(data_start, len(grid)):
        row = grid[row_idx]
        if not any(c.strip() for c in row):
            continue
        row_dict: dict = {}
        for col_idx, field in col_types.items():
            if col_idx < len(row) and row[col_idx].strip():
                row_dict[field] = row[col_idx].strip()
        if not row_dict:
            continue
        _refine_row_from_joined(row_dict, " ".join(c for c in row if c.strip()))
        row_dict["_row_index"] = row_idx - data_start
        rows.append(row_dict)

    if not rows:
        return rows

    validated = []
    for r in rows:
        if r.get("trade_date") or r.get("fund_name"):
            validated.append(r)
    return validated or rows


_PLATFORM_NAMES = {"蚂蚁基金", "腾安基金", "天天基金", "支付宝", "理财通", "微众银行", "京东金融", "同花顺", "东方财富", "且慢", "蛋卷基金", "好买基金", "众禄基金", "数米基金", "长量基金", "上海天天基金", "北京同花顺"}

_REJECT_COL_KEYWORDS = ["尾号", "银行", "账户", "渠道", "钱包", "APP", "e钱包"]


def _is_fund_name_value(v: str) -> bool:
    if len(v) < 6 or not any("\u4e00" <= c <= "\u9fff" for c in v):
        return False
    if re.match(r"^[\d\s:.,\-/()（）%]+$", v):
        return False
    if any(kw in v for kw in _REJECT_COL_KEYWORDS):
        return False
    if v in _PLATFORM_NAMES:
        return False
    return True


def _is_action_value(v: str) -> bool:
    return any(w in v for w in ("申购", "赎回", "买入", "卖出", "红利再投", "现金分红", "分红", "转换", "强制调增", "强制调减"))


def _is_status_value(v: str) -> bool:
    return any(w in v for w in ("成功", "失败", "已确认", "确认成功", "撤销", "已撤销", "撤单", "已取消", "作废"))


_DATE_LIKE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")
_DATE_CONCAT = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}\d{1,2}:\d{2}")


def _is_date_value(v: str) -> bool:
    return bool(_DATE_LIKE.match(v)) or bool(_DATE_CONCAT.match(v))


def _is_number_value(v: str) -> bool:
    try:
        float(v.replace(",", ""))
        return True
    except ValueError:
        return False


def _identify_column_types(grid: list[list[str]]) -> dict[int, str]:
    if not grid:
        return {}
    ncols = max(len(row) for row in grid)
    col_types: dict[int, str] = {}
    assigned: set[str] = set()

    values_by_col: dict[int, list[str]] = {}
    for col in range(ncols):
        vals = []
        for row in grid:
            if col < len(row) and row[col].strip():
                vals.append(row[col].strip())
        values_by_col[col] = vals

    for col in range(ncols):
        vals = values_by_col[col]
        if not vals:
            continue

        action_ratio = sum(1 for v in vals if _is_action_value(v)) / len(vals)
        status_ratio = sum(1 for v in vals if _is_status_value(v)) / len(vals)
        date_ratio = sum(1 for v in vals if _is_date_value(v)) / len(vals)
        num_vals = [v for v in vals if _is_number_value(v)]

        if action_ratio > 0.35 and "action" not in assigned:
            col_types[col] = "action"
            assigned.add("action")
        elif status_ratio > 0.35 and "status" not in assigned:
            col_types[col] = "status"
            assigned.add("status")
        elif date_ratio > 0.5 and "trade_date" not in assigned:
            col_types[col] = "trade_date"
            assigned.add("trade_date")

    name_candidates: list[tuple[int, float]] = []
    for col in range(ncols):
        vals = values_by_col[col]
        if col in col_types or not vals:
            continue
        name_vals = [v for v in vals if _is_fund_name_value(v)]
        if len(name_vals) < 2:
            continue
        avg_len = sum(len(v) for v in name_vals) / len(name_vals)
        name_candidates.append((col, avg_len))

    if name_candidates:
        best_col = max(name_candidates, key=lambda x: x[1])[0]
        if best_col is not None:
            col_types[best_col] = "fund_name"
            assigned.add("fund_name")

    remaining = [c for c in range(ncols) if c not in col_types]
    for col in remaining:
        vals = values_by_col[col]
        num_vals = [v for v in vals if _is_number_value(v)]
        if len(num_vals) < 2:
            continue
        nums = [float(v.replace(",", "")) for v in num_vals]
        avg = sum(nums) / len(nums)
        if "share" not in assigned and all(n < 10000 for n in nums) and any(n < 1000 for n in nums):
            col_types[col] = "share"
            assigned.add("share")
        elif "amount" not in assigned:
            col_types[col] = "amount_cny"
            assigned.add("amount_cny")

    return col_types


def _find_data_start(grid: list[list[str]], col_types: dict[int, str]) -> int:
    for row_idx, row in enumerate(grid):
        if not any(c.strip() for c in row):
            continue
        has_action = False
        has_date = False
        for col_idx, field in col_types.items():
            if col_idx < len(row):
                val = row[col_idx].strip()
                if field == "action" and any(w in val for w in ("申购", "赎回", "买入", "卖出", "分红")):
                    has_action = True
                if field == "trade_date" and re.match(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", val):
                    has_date = True
            if has_action and has_date:
                return row_idx
    return 1 if len(grid) > 1 else 0


def _refine_row_from_joined(row_dict: dict, joined: str) -> None:
    if not row_dict.get("action"):
        if any(w in joined for w in ("赎回", "卖出")):
            row_dict["action"] = "sell"
        elif "红利再投" in joined:
            row_dict["action"] = "dividend_reinvest"
        elif "分红" in joined:
            row_dict["action"] = "dividend"
        else:
            row_dict["action"] = "buy"

    raw_date = row_dict.get("trade_date", "")
    if raw_date:
        m = re.match(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", raw_date.replace(" ", ""))
        if m:
            row_dict["trade_date"] = m.group(1).replace("/", "-")
    else:
        m = _DATE_CONCAT.search(joined) or _DATE_LIKE.search(joined)
        if m:
            raw = m.group(0)
            row_dict["trade_date"] = raw[:10].replace("/", "-")

    if not row_dict.get("submitted_at"):
        m = re.search(r"(\d{1,2}:\d{2})", joined)
        if m:
            row_dict["submitted_at"] = m.group(1)

    if not row_dict.get("fund_code"):
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", joined)
        if m:
            row_dict["fund_code"] = m.group(1)

    for field in ("share", "amount_cny"):
        val = row_dict.get(field)
        if val in ("--", "-", "", None):
            row_dict.pop(field, None)

    raw_action = row_dict.get("action", "")
    if raw_action in ("申购", "买入", "buy"):
        row_dict["action"] = "buy"
    elif raw_action in ("赎回", "卖出", "sell"):
        row_dict["action"] = "sell"
    elif "红利再投" in raw_action or raw_action == "dividend_reinvest":
        row_dict["action"] = "dividend_reinvest"
    elif "分红" in raw_action or raw_action == "dividend":
        row_dict["action"] = "dividend"

    action = row_dict.get("action", "buy")
    if action == "sell" and "share" not in row_dict:
        cleaned = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}\s*\d{1,2}:\d{2}(?::\d{2})?", " ", joined)
        cleaned = re.sub(r"\d{1,2}:\d{2}(?::\d{2})?", " ", cleaned)
        cleaned = re.sub(r"尾号\d+", " ", cleaned)
        cleaned = re.sub(r"\d{4}(?=\s|$)", " ", cleaned)
        numbers = re.findall(r"(\d+(?:\.\d{1,2})?)", cleaned)
        for n_str in numbers:
            try:
                v = float(n_str)
            except ValueError:
                continue
            if 10 <= v < 100000:
                row_dict["share"] = n_str
                break
    elif action != "sell" and "amount_cny" not in row_dict:
        cleaned = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}\s*\d{1,2}:\d{2}(?::\d{2})?", " ", joined)
        cleaned = re.sub(r"\d{1,2}:\d{2}(?::\d{2})?", " ", cleaned)
        cleaned = re.sub(r"尾号\d+", " ", cleaned)
        numbers = re.findall(r"(\d+(?:\.\d{1,2})?)", cleaned)
        for n_str in numbers:
            try:
                v = float(n_str)
            except ValueError:
                continue
            if 10 <= v < 100000:
                row_dict["amount_cny"] = n_str
                break


def _flatten_result(result) -> OcrResult:
    texts: list[str] = []
    confidences: list[float] = []

    def visit(node):
        if node is None:
            return
        if isinstance(node, dict):
            if "rec_texts" in node:
                texts.extend(str(x) for x in node.get("rec_texts") or [])
            if "rec_scores" in node:
                confidences.extend(float(x) for x in node.get("rec_scores") or [])
            for value in node.values():
                visit(value)
            return
        if isinstance(node, tuple) and len(node) == 2 and isinstance(node[1], (int, float)):
            texts.append(str(node[0]))
            confidences.append(float(node[1]))
            return
        if isinstance(node, list):
            if len(node) == 3 and isinstance(node[1], str):
                texts.append(str(node[1]))
                if isinstance(node[2], (int, float)):
                    confidences.append(float(node[2]))
                return
            if len(node) == 2 and isinstance(node[1], tuple):
                text, score = node[1]
                texts.append(str(text))
                confidences.append(float(score))
                return
            for value in node:
                visit(value)

    visit(result)
    confidence = sum(confidences) / len(confidences) if confidences else None
    return OcrResult(text="\n".join(texts), confidence=confidence)


def _config_value(config: dict[str, str] | None, key: str, default: str = "") -> str:
    if config is not None:
        return config.get(key, default)
    return os.getenv(key, default)


def _value_by_path(data, path: str):
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            return None
    return current

from dataclasses import dataclass
import base64
from html.parser import HTMLParser
from pathlib import Path
import threading
import time

import requests


@dataclass
class OcrResult:
    text: str
    rows: list[list[str]]


_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}


def recognize_file(path: str | Path, config: dict[str, str] | None = None) -> OcrResult:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(str(file_path))
    cfg = config or {}
    if _config_value(cfg, "OCR_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("OCR is disabled")
    return _recognize_with_baidu_table(file_path, cfg)


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

    access_token = _get_baidu_access_token(api_key, secret_key)
    response = requests.post(
        endpoint,
        params={"access_token": access_token},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"image": base64.b64encode(path.read_bytes()).decode("ascii")},
        timeout=90,
    )
    response.raise_for_status()
    data = response.json()
    if "error_code" in data:
        raise RuntimeError(f"百度表格OCR API 返回错误: {data.get('error_code')} - {data.get('error_msg', '')}")

    rows = _extract_table_rows(data)
    if not rows:
        raise RuntimeError("百度表格OCR未返回可解析表格")
    text = "\n".join(" ".join(cell for cell in row if cell) for row in rows)
    return OcrResult(text=text, rows=rows)


def _get_baidu_access_token(api_key: str, secret_key: str) -> str:
    cache_key = (api_key, secret_key)
    now = time.time()
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and cached[1] > now + 60:
            return cached[0]
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
        payload = token_response.json()
        access_token = payload.get("access_token")
        if not access_token:
            raise RuntimeError("Baidu OCR access token response did not include access_token")
        expires_in = int(payload.get("expires_in") or 2592000)
        _TOKEN_CACHE[cache_key] = (access_token, time.time() + max(expires_in - 300, 60))
        return access_token


def _extract_table_rows(data: dict) -> list[list[str]]:
    body: list[dict] = []
    for table in data.get("tables_result") or []:
        if isinstance(table, dict):
            body.extend(table.get("body") or [])
    if body:
        return _cells_to_grid(body)

    html_data = None
    result = data.get("result")
    if isinstance(result, dict):
        html_data = result.get("data")
    elif isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and item.get("data"):
                html_data = item["data"]
                break
    return _parse_table_html(html_data) if html_data else []


def _cells_to_grid(body: list[dict]) -> list[list[str]]:
    max_row = 0
    max_col = 0
    for cell in body:
        max_row = max(max_row, int(cell.get("row_end", 0)))
        max_col = max(max_col, int(cell.get("col_end", 0)))
    grid = [["" for _ in range(max_col)] for _ in range(max_row)]
    for cell in body:
        r = int(cell.get("row_start", 0))
        c = int(cell.get("col_start", 0))
        if r < max_row and c < max_col:
            grid[r][c] = (cell.get("words") or cell.get("cell") or "").strip()
    return grid


def _parse_table_html(html: str) -> list[list[str]]:
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows: list[list[str]] = []
            self.row: list[str] = []
            self.capture = False

        def handle_starttag(self, tag, attrs):
            if tag in {"td", "th"}:
                self.capture = True
                self.row.append("")

        def handle_endtag(self, tag):
            if tag in {"td", "th"}:
                self.capture = False
            elif tag == "tr" and self.row:
                self.rows.append(self.row)
                self.row = []

        def handle_data(self, data):
            if self.capture and self.row:
                self.row[-1] += data.strip()

    parser = Parser()
    parser.feed(html)
    if parser.row:
        parser.rows.append(parser.row)
    return parser.rows


def _config_value(config: dict[str, str], key: str, default: str = "") -> str:
    value = config.get(key)
    if value not in (None, ""):
        return str(value)
    import os
    return os.getenv(key, default)

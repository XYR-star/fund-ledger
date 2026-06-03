from dataclasses import dataclass
import base64
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

import requests


@dataclass
class OcrResult:
    text: str
    confidence: float | None


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
    if backend == "api":
        return _recognize_with_api(file_path, config or {})
    if backend == "baidu":
        return _recognize_with_baidu(file_path, config or {})
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


def _recognize_with_baidu(path: Path, config: dict[str, str]) -> OcrResult:
    api_key = _config_value(config, "BAIDU_OCR_API_KEY")
    secret_key = _config_value(config, "BAIDU_OCR_SECRET_KEY")
    endpoint = _config_value(
        config,
        "BAIDU_OCR_ENDPOINT",
        "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic",
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
        data={"image": base64.b64encode(path.read_bytes()).decode("ascii")},
        timeout=90,
    )
    response.raise_for_status()
    data = response.json()
    words = data.get("words_result") or []
    return OcrResult(text="\n".join(str(item.get("words", "")) for item in words), confidence=None)


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

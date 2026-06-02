from dataclasses import dataclass
import os
from pathlib import Path
from tempfile import TemporaryDirectory


@dataclass
class OcrResult:
    text: str
    confidence: float | None


_ENGINE = None
_BACKEND = None


def recognize_file(path: str | Path) -> OcrResult:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(str(file_path))
    if file_path.suffix.lower() == ".pdf":
        return _recognize_pdf(file_path)
    return _recognize_image(file_path)


def _get_engine():
    global _BACKEND, _ENGINE
    if _ENGINE is None:
        backend = os.getenv("OCR_BACKEND", "rapidocr").lower()
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


def _recognize_image(path: Path) -> OcrResult:
    backend, engine = _get_engine()
    if backend == "rapidocr":
        result, _ = engine(str(path))
    else:
        result = engine.ocr(str(path), cls=True)
    return _flatten_result(result)


def _recognize_pdf(path: Path) -> OcrResult:
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
            item = _recognize_image(image_path)
            if item.text:
                texts.append(item.text)
            if item.confidence is not None:
                confidences.append(item.confidence)
    confidence = sum(confidences) / len(confidences) if confidences else None
    return OcrResult(text="\n".join(texts), confidence=confidence)


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

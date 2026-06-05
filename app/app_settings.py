import os
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from sqlmodel import Session, select

from .models import AppSetting


SECRET_KEYS = {
    "DEEPSEEK_API_KEY",
    "OCR_API_KEY",
    "BAIDU_OCR_API_KEY",
    "BAIDU_OCR_SECRET_KEY",
}

DEFAULTS = {
    "DEEPSEEK_ENABLED": "true",
    "DEEPSEEK_API_KEY": "",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "DEEPSEEK_MODEL": "deepseek-chat",
    "OCR_ENABLED": "true",
    "OCR_BACKEND": "rapidocr",
    "OCR_API_PROVIDER": "generic",
    "OCR_API_URL": "",
    "OCR_API_AUTH_HEADER": "Authorization",
    "OCR_API_AUTH_PREFIX": "Bearer ",
    "OCR_API_KEY": "",
    "OCR_API_FILE_FIELD": "file",
    "OCR_API_TEXT_PATH": "text",
    "BAIDU_OCR_API_KEY": "",
    "BAIDU_OCR_SECRET_KEY": "",
    "BAIDU_OCR_ENDPOINT": "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic",
    "AUTO_MARKET_SYNC_ENABLED": "true",
    "AUTO_MARKET_SYNC_TIME": "21:30",
    "AUTO_MARKET_SYNC_TIMEZONE": "Asia/Shanghai",
    "AUTO_MARKET_SYNC_LAST_RUN_DATE": "",
}


def runtime_settings(session: Session) -> dict[str, str]:
    data = {key: os.getenv(key, default) for key, default in DEFAULTS.items()}
    rows = session.exec(select(AppSetting)).all()
    for row in rows:
        if row.value != "":
            data[row.key] = row.value
    return data


def save_settings(session: Session, values: dict[str, str]) -> None:
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        existing = session.get(AppSetting, key)
        if existing:
            existing.value = value
            existing.is_secret = key in SECRET_KEYS
            existing.updated_at = datetime.utcnow()
            session.add(existing)
        else:
            session.add(
                AppSetting(
                    key=key,
                    value=value,
                    is_secret=key in SECRET_KEYS,
                    updated_at=datetime.utcnow(),
                )
            )
    session.commit()


def masked(value: str) -> str:
    if not value:
        return "未配置"
    if len(value) <= 8:
        return "已配置"
    return f"已配置，末尾 {value[-4:]}"


def configured(value: str) -> bool:
    return bool(value.strip())


@contextmanager
def temporary_environ(values: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value == "":
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

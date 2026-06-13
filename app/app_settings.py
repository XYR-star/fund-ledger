import os
from datetime import datetime

from sqlmodel import Session, select

from .models import AppSetting


SECRET_KEYS = {
    "BAIDU_OCR_API_KEY",
    "BAIDU_OCR_SECRET_KEY",
}

DEFAULTS = {
    "OCR_ENABLED": "true",
    "OCR_BACKEND": "baidu_table",
    "BAIDU_OCR_API_KEY": "",
    "BAIDU_OCR_SECRET_KEY": "",
    "BAIDU_TABLE_OCR_ENDPOINT": "https://aip.baidubce.com/rest/2.0/ocr/v1/table",
    "NAV_SYNC_ENABLED": "false",
    "NAV_SYNC_TIME": "18:30",
    "NAV_SYNC_PZ": "40000",
    "NAV_SYNC_LAST_RUN_DATE": "",
    "NAV_SYNC_LAST_RUN_AT": "",
    "NAV_SYNC_LAST_RESULT": "",
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

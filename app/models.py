from datetime import datetime
from enum import Enum

from sqlmodel import Field, SQLModel


class ImportStatus(str, Enum):
    uploaded = "uploaded"
    ocr_running = "ocr_running"
    ocr_done = "ocr_done"
    error = "error"


class ImportDocument(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    file_name: str | None = None
    source_file: str | None = None
    source_hash: str | None = Field(default=None, index=True)
    content_type: str | None = None
    status: ImportStatus = Field(default=ImportStatus.uploaded, index=True)
    raw_text: str = ""
    ocr_text: str = ""
    error_message: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AppSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""
    is_secret: bool = False
    updated_at: datetime = Field(default_factory=datetime.utcnow)

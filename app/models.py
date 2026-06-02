from datetime import date, datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel, UniqueConstraint


class CandidateStatus(str, Enum):
    pending = "pending"
    confirmed = "confirmed"
    ignored = "ignored"


class TransactionAction(str, Enum):
    buy = "buy"
    sell = "sell"
    dividend = "dividend"
    dividend_reinvest = "dividend_reinvest"
    fee_adjustment = "fee_adjustment"


class ImportStatus(str, Enum):
    uploaded = "uploaded"
    ocr_running = "ocr_running"
    ocr_done = "ocr_done"
    parse_done = "parse_done"
    archived = "archived"
    deleted = "deleted"
    error = "error"


class AppSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""
    is_secret: bool = False
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FundRule(SQLModel, table=True):
    fund_code: str = Field(primary_key=True, max_length=6)
    fund_name: str = ""
    buy_confirm_days: int = 1
    sell_confirm_days: int = 1
    cutoff_time: str = "15:00"
    buy_fee_rate: float = 0.0
    sync_source: str = ""
    synced_at: Optional[datetime] = None
    notes: str = ""
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FundFeeTier(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    fund_code: str = Field(index=True, max_length=6)
    min_holding_days: int = 0
    max_holding_days: Optional[int] = None
    redemption_fee_rate: float = 0.0
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ImportDocument(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    file_name: Optional[str] = None
    source_file: Optional[str] = None
    source_hash: Optional[str] = Field(default=None, index=True)
    content_type: Optional[str] = None
    status: ImportStatus = Field(default=ImportStatus.uploaded, index=True)
    raw_text: str = ""
    ocr_text: str = ""
    error_message: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FundTransactionCandidate(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    status: CandidateStatus = Field(default=CandidateStatus.pending, index=True)
    fund_code: str = Field(index=True, max_length=6)
    fund_name: str = ""
    trade_date: date
    confirm_date: Optional[date] = None
    action: TransactionAction = Field(default=TransactionAction.buy)
    amount_cny: Optional[float] = None
    share: Optional[float] = None
    nav: Optional[float] = None
    fee: Optional[float] = None
    source_file: Optional[str] = None
    source_hash: Optional[str] = Field(default=None, index=True)
    raw_text: str = ""
    confidence: float = 0.5
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    confirmed_transaction_id: Optional[int] = None


class FundTransaction(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: Optional[int] = Field(default=None, index=True, unique=True)
    fund_code: str = Field(index=True, max_length=6)
    fund_name: str = ""
    trade_date: date = Field(index=True)
    confirm_date: Optional[date] = None
    action: TransactionAction = Field(default=TransactionAction.buy)
    amount_cny: Optional[float] = None
    share: Optional[float] = None
    nav: Optional[float] = None
    fee: Optional[float] = None
    source_file: Optional[str] = None
    raw_text: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FundNav(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("fund_code", "nav_date"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    fund_code: str = Field(index=True, max_length=6)
    nav_date: date = Field(index=True)
    unit_nav: float
    accumulated_nav: Optional[float] = None
    daily_return: Optional[float] = None
    source: str = "efinance"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

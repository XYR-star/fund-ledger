from datetime import date, datetime, time
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel, UniqueConstraint

from .timezone import now_shanghai_naive


class FundType(str, Enum):
    open_fund = "open_fund"
    etf = "etf"
    money_fund = "money_fund"
    unknown = "unknown"


class TransactionAction(str, Enum):
    buy = "buy"
    sell = "sell"
    dividend = "dividend"
    dividend_reinvest = "dividend_reinvest"


class EventType(str, Enum):
    dividend_method = "dividend_method"
    sip_start = "sip_start"
    sip_stop = "sip_stop"
    sip_update = "sip_update"
    ignored_status = "ignored_status"
    other = "other"


class ImportStatus(str, Enum):
    uploaded = "uploaded"
    ocr_running = "ocr_running"
    ocr_done = "ocr_done"
    parsed = "parsed"
    error = "error"
    archived = "archived"


class CandidateStatus(str, Enum):
    pending = "pending"
    needs_review = "needs_review"
    auto_ready = "auto_ready"
    posted = "posted"
    event = "event"
    ignored = "ignored"


class RowStatus(str, Enum):
    success = "success"
    cancelled = "cancelled"
    failed = "failed"
    unknown = "unknown"


class ImportDocument(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    file_name: Optional[str] = None
    source_file: Optional[str] = None
    source_hash: Optional[str] = Field(default=None, index=True)
    content_type: Optional[str] = None
    status: ImportStatus = Field(default=ImportStatus.uploaded, index=True)
    ocr_text: str = ""
    error_message: str = ""
    created_at: datetime = Field(default_factory=now_shanghai_naive)
    updated_at: datetime = Field(default_factory=now_shanghai_naive)


class OcrRow(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("document_id", "row_index", "row_hash"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(index=True)
    row_index: int = Field(index=True)
    row_hash: str = Field(index=True)
    raw_json: str = ""
    raw_text: str = ""
    parsed_at: datetime = Field(default_factory=now_shanghai_naive)


class FundAlias(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    keyword: str = Field(index=True)
    fund_code: str = Field(index=True, max_length=6)
    fund_name: str = ""
    fund_type: FundType = Field(default=FundType.unknown, index=True)
    source: str = "manual"
    created_at: datetime = Field(default_factory=now_shanghai_naive)


class FundRule(SQLModel, table=True):
    fund_code: str = Field(primary_key=True, max_length=6)
    fund_name: str = ""
    fund_type: FundType = Field(default=FundType.unknown, index=True)
    buy_confirm_days: int = 1
    sell_confirm_days: int = 1
    cutoff_time: str = "15:00"
    buy_fee_rate: float = 0.0
    platform: str = ""
    dividend_method: str = ""
    sync_source: str = ""
    synced_at: Optional[datetime] = None
    notes: str = ""
    updated_at: datetime = Field(default_factory=now_shanghai_naive)


class FundFeeTier(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    fund_code: str = Field(index=True, max_length=6)
    min_holding_days: int = 0
    max_holding_days: Optional[int] = None
    redemption_fee_rate: float = 0.0


class TransactionCandidate(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: Optional[int] = Field(default=None, index=True)
    ocr_row_id: Optional[int] = Field(default=None, index=True)
    row_hash: str = Field(default="", index=True)
    status: CandidateStatus = Field(default=CandidateStatus.pending, index=True)
    row_status: RowStatus = Field(default=RowStatus.unknown, index=True)
    action: Optional[TransactionAction] = Field(default=None, index=True)
    event_type: Optional[EventType] = Field(default=None, index=True)
    fund_code: str = Field(default="", index=True, max_length=6)
    fund_name: str = ""
    fund_type: FundType = Field(default=FundType.unknown, index=True)
    trade_date: Optional[date] = Field(default=None, index=True)
    submitted_at: Optional[time] = None
    effective_nav_date: Optional[date] = None
    confirm_date: Optional[date] = None
    amount_cny: Optional[float] = None
    share: Optional[float] = None
    nav: Optional[float] = None
    fee: Optional[float] = None
    confidence: float = 0.0
    review_reason: str = ""
    raw_text: str = ""
    posted_transaction_id: Optional[int] = None
    posted_event_id: Optional[int] = None
    created_at: datetime = Field(default_factory=now_shanghai_naive)
    updated_at: datetime = Field(default_factory=now_shanghai_naive)


class CandidateIssue(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("candidate_id", "code"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: int = Field(index=True)
    code: str = Field(index=True)
    severity: str = Field(default="error", index=True)
    message: str = ""
    detail: str = ""
    created_at: datetime = Field(default_factory=now_shanghai_naive)


class FundTransaction(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: Optional[int] = Field(default=None, index=True)
    fund_code: str = Field(index=True, max_length=6)
    fund_name: str = ""
    fund_type: FundType = Field(default=FundType.open_fund, index=True)
    trade_date: date = Field(index=True)
    submitted_at: Optional[time] = None
    effective_nav_date: Optional[date] = None
    confirm_date: Optional[date] = None
    action: TransactionAction = Field(default=TransactionAction.buy, index=True)
    amount_cny: Optional[float] = None
    share: Optional[float] = None
    nav: Optional[float] = None
    fee: Optional[float] = None
    source_file: Optional[str] = None
    raw_text: str = ""
    created_at: datetime = Field(default_factory=now_shanghai_naive)


class FundEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: Optional[int] = Field(default=None, index=True)
    event_type: EventType = Field(default=EventType.other, index=True)
    fund_code: str = Field(default="", index=True, max_length=6)
    fund_name: str = ""
    fund_type: FundType = Field(default=FundType.unknown, index=True)
    event_date: Optional[date] = Field(default=None, index=True)
    submitted_at: Optional[time] = None
    amount_cny: Optional[float] = None
    note: str = ""
    raw_text: str = ""
    created_at: datetime = Field(default_factory=now_shanghai_naive)


class FundNav(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("fund_code", "nav_date"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    fund_code: str = Field(index=True, max_length=6)
    nav_date: date = Field(index=True)
    unit_nav: float
    accumulated_nav: Optional[float] = None
    daily_return: Optional[float] = None
    source: str = "efinance"
    created_at: datetime = Field(default_factory=now_shanghai_naive)
    updated_at: datetime = Field(default_factory=now_shanghai_naive)


class EAccountImport(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    file_name: str = ""
    source_hash: str = Field(default="", index=True)
    row_count: int = 0
    matched_count: int = 0
    mismatch_count: int = 0
    missing_count: int = 0
    imported_at: datetime = Field(default_factory=now_shanghai_naive)


class EAccountHolding(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    import_id: int = Field(index=True)
    fund_code: str = Field(default="", index=True, max_length=6)
    fund_name: str = ""
    fund_account: str = ""
    official_share: Optional[float] = None
    share_date: Optional[date] = None
    nav: Optional[float] = None
    nav_date: Optional[date] = None
    official_market_value: Optional[float] = None
    settlement_value: Optional[float] = None
    local_share: Optional[float] = None
    local_market_value: Optional[float] = None
    share_diff: Optional[float] = None
    market_value_diff: Optional[float] = None
    status: str = Field(default="unknown", index=True)
    issue_summary: str = ""


class AppSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""
    is_secret: bool = False
    updated_at: datetime = Field(default_factory=now_shanghai_naive)

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .models import TransactionAction


@dataclass
class ExtractedCandidate:
    fund_code: str
    fund_name: str
    trade_date: date
    confirm_date: date | None
    action: TransactionAction
    amount_cny: float | None
    share: float | None
    nav: float | None
    fee: float | None
    raw_text: str
    confidence: float


def hash_content(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def hash_file(path: Path) -> str:
    return hash_content(path.read_bytes())


def extract_candidates(raw_text: str) -> list[ExtractedCandidate]:
    """Manual text extractor placeholder.

    Expected line format, with forgiving separators:
    2024-01-02 161725 招商中证白酒 buy 1000 0 0 0

    Fields: trade_date fund_code fund_name action amount share nav fee
    Fund name may be "-". Amount/share/nav/fee may be blank or "-".
    """
    candidates: list[ExtractedCandidate] = []
    for line in raw_text.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        parts = re.split(r"[\s,，]+", text)
        if len(parts) < 4:
            continue
        try:
            trade_date = date.fromisoformat(parts[0].replace("/", "-"))
        except ValueError:
            continue
        fund_code = parts[1].zfill(6)
        if not re.fullmatch(r"\d{6}", fund_code):
            continue
        fund_name = "" if parts[2] == "-" else parts[2]
        action = _parse_action(parts[3])
        values = parts[4:] + ["-", "-", "-", "-"]
        amount = _parse_float(values[0])
        share = _parse_float(values[1])
        nav = _parse_float(values[2])
        fee = _parse_float(values[3])
        candidates.append(
            ExtractedCandidate(
                fund_code=fund_code,
                fund_name=fund_name,
                trade_date=trade_date,
                confirm_date=None,
                action=action,
                amount_cny=amount,
                share=share,
                nav=nav,
                fee=fee,
                raw_text=text,
                confidence=0.7,
            )
        )
    return candidates


def _parse_action(value: str) -> TransactionAction:
    normalized = value.lower()
    mapping = {
        "申购": TransactionAction.buy,
        "买入": TransactionAction.buy,
        "buy": TransactionAction.buy,
        "赎回": TransactionAction.sell,
        "卖出": TransactionAction.sell,
        "sell": TransactionAction.sell,
        "分红": TransactionAction.dividend,
        "dividend": TransactionAction.dividend,
        "红利再投": TransactionAction.dividend_reinvest,
        "dividend_reinvest": TransactionAction.dividend_reinvest,
    }
    return mapping.get(normalized, TransactionAction.buy)


def _parse_float(value: str) -> float | None:
    if value in {"", "-", "null", "None"}:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None

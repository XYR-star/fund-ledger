from dataclasses import dataclass

from sqlmodel import Session, select

from .models import CandidateIssue, TransactionCandidate


@dataclass(frozen=True)
class CandidateIssueItem:
    code: str
    message: str
    severity: str = "error"
    detail: str = ""


def set_candidate_issues(session: Session, candidate: TransactionCandidate, issues: list[CandidateIssueItem]) -> None:
    candidate.review_reason = "；".join(issue.message for issue in issues)
    if not candidate.id:
        return
    existing = session.exec(select(CandidateIssue).where(CandidateIssue.candidate_id == candidate.id)).all()
    by_code = {issue.code: issue for issue in existing}
    wanted_codes = {issue.code for issue in issues}
    for old in existing:
        if old.code not in wanted_codes:
            session.delete(old)
    for item in issues:
        row = by_code.get(item.code)
        if row:
            row.severity = item.severity
            row.message = item.message
            row.detail = item.detail
            session.add(row)
        else:
            session.add(
                CandidateIssue(
                    candidate_id=candidate.id,
                    code=item.code,
                    severity=item.severity,
                    message=item.message,
                    detail=item.detail,
                )
            )


def issue(code: str, message: str, severity: str = "error", detail: str = "") -> CandidateIssueItem:
    return CandidateIssueItem(code=code, message=message, severity=severity, detail=detail)

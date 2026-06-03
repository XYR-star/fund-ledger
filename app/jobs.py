import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable

from sqlmodel import Session, select

from .models import BackgroundJob, JobStatus


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fund-ledger-job")
_handlers: dict[str, Callable[[dict[str, Any]], str]] = {}


def register_job(job_type: str, handler: Callable[[dict[str, Any]], str]) -> None:
    _handlers[job_type] = handler


def create_job(session: Session, job_type: str, payload: dict[str, Any]) -> BackgroundJob:
    job = BackgroundJob(job_type=job_type, payload_json=json.dumps(payload, ensure_ascii=False))
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def enqueue_job(job_id: int) -> None:
    _executor.submit(run_job, job_id)


def create_and_enqueue(session: Session, job_type: str, payload: dict[str, Any]) -> BackgroundJob:
    job = create_job(session, job_type, payload)
    enqueue_job(job.id)
    return job


def recover_interrupted_jobs() -> None:
    queued_ids = []
    with Session(_engine()) as session:
        jobs = session.exec(select(BackgroundJob).where(BackgroundJob.status == JobStatus.running)).all()
        for job in jobs:
            job.status = JobStatus.error
            job.error_message = "任务在服务重启或进程退出时中断"
            job.finished_at = datetime.utcnow()
            session.add(job)
        queued_ids = [
            job.id
            for job in session.exec(select(BackgroundJob).where(BackgroundJob.status == JobStatus.queued)).all()
            if job.id is not None
        ]
        session.commit()
    for job_id in queued_ids:
        enqueue_job(job_id)


def recent_jobs(session: Session, limit: int = 10) -> list[BackgroundJob]:
    return session.exec(select(BackgroundJob).order_by(BackgroundJob.created_at.desc()).limit(limit)).all()


def run_job(job_id: int) -> None:
    with Session(_engine()) as session:
        job = session.get(BackgroundJob, job_id)
        if not job or job.status != JobStatus.queued:
            return
        job.status = JobStatus.running
        job.started_at = datetime.utcnow()
        session.add(job)
        session.commit()

    try:
        message = _run_handler(job_id)
    except Exception as exc:
        with Session(_engine()) as session:
            job = session.get(BackgroundJob, job_id)
            if job:
                job.status = JobStatus.error
                job.error_message = str(exc)
                job.finished_at = datetime.utcnow()
                session.add(job)
                session.commit()
        return

    with Session(_engine()) as session:
        job = session.get(BackgroundJob, job_id)
        if job:
            job.status = JobStatus.done
            job.result_message = message
            job.finished_at = datetime.utcnow()
            session.add(job)
            session.commit()


def _run_handler(job_id: int) -> str:
    with Session(_engine()) as session:
        job = session.get(BackgroundJob, job_id)
        if not job:
            raise RuntimeError(f"job {job_id} not found")
        handler = _handlers.get(job.job_type)
        if not handler:
            raise RuntimeError(f"unknown job type: {job.job_type}")
        payload = json.loads(job.payload_json or "{}")
    return handler(payload)


def _engine():
    from .db import engine

    return engine

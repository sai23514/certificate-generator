"""Business logic: creating jobs, generating certificates, summarising progress."""
import logging
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.certificate import CertificateData, CertificateRenderError, render_certificate
from app.config import settings
from app.database import SessionLocal
from app.models import (
    TERMINAL_JOB_STATUSES,
    Certificate,
    CertificateStatus,
    Job,
    JobStatus,
    utcnow,
)
from app.schemas import Counts, FailureOut, JobCreate, JobDetail, RecipientIn

logger = logging.getLogger(__name__)

FAILURE_PREVIEW_LIMIT = 50


# ------------------------------------------------------------ job creation

def validate_recipient(raw: Any) -> tuple[RecipientIn | None, str | None]:
    """Return (recipient, None) if valid, else (None, human-readable error)."""
    if not isinstance(raw, dict):
        return None, "Recipient must be an object with a 'name' and an optional 'email'."
    try:
        return RecipientIn.model_validate(raw), None
    except ValidationError as exc:
        parts = []
        for err in exc.errors():
            field = ".".join(str(p) for p in err["loc"]) or "recipient"
            parts.append(f"{field}: {err['msg'].removeprefix('Value error, ')}")
        return None, "; ".join(parts)


def _display_value(raw: Any, key: str) -> str | None:
    if isinstance(raw, dict) and isinstance(raw.get(key), str):
        return raw[key][:255]
    return None


def create_job(db: Session, payload: JobCreate) -> tuple[Job, bool]:
    """Persist the job and one row per recipient.

    Invalid recipients are stored as already-failed rows (with the reason), so the
    client sees them in the job status. Returns (job, needs_processing).
    """
    now = utcnow()
    job = Job(
        id=str(uuid.uuid4()),
        status=JobStatus.PENDING.value,
        certificate_title=payload.certificate_title,
        course_name=payload.course_name,
        issuer_name=payload.issuer_name,
        issue_date=payload.issue_date or now.date(),
        total=len(payload.recipients),
        created_at=now,
    )
    db.add(job)
    db.flush()  # make sure the job row exists before certificates reference it

    valid_count = 0
    certificates = []
    for index, raw in enumerate(payload.recipients, start=1):
        recipient, error = validate_recipient(raw)
        cert = Certificate(
            id=str(uuid.uuid4()),
            job_id=job.id,
            row_index=index,
            raw_data=raw,
            created_at=now,
        )
        if recipient is None:
            cert.status = CertificateStatus.FAILED.value
            cert.error = error
            cert.recipient_name = _display_value(raw, "name")
            cert.recipient_email = _display_value(raw, "email")
        else:
            cert.status = CertificateStatus.PENDING.value
            cert.recipient_name = recipient.name
            cert.recipient_email = str(recipient.email) if recipient.email else None
            valid_count += 1
        certificates.append(cert)

    needs_processing = valid_count > 0
    if not needs_processing:  # nothing to generate: the job is already finished
        job.status = JobStatus.FAILED.value
        job.completed_at = now

    db.add_all(certificates)
    db.commit()
    return job, needs_processing


# -------------------------------------------------------------- processing

def process_job(job_id: str) -> None:
    """Generate every pending certificate of a job. Runs in a background thread.

    * Each certificate is committed on its own, so progress is visible while running
      and a failure on one recipient is recorded without affecting the others.
    * Only PENDING rows are touched, so calling this twice is harmless (idempotent).
    """
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            logger.warning("process_job: job %s not found", job_id)
            return
        if job.status in TERMINAL_JOB_STATUSES:
            return

        job.status = JobStatus.PROCESSING.value
        job.started_at = job.started_at or utcnow()
        db.commit()

        try:
            _generate_pending(db, job)
        except Exception as exc:  # the job itself broke (e.g. database went away)
            logger.exception("Job %s aborted", job_id)
            db.rollback()
            job = db.get(Job, job_id)
            job.status = JobStatus.FAILED.value
            job.error = f"Processing aborted: {type(exc).__name__}: {exc}"[:1000]
            job.completed_at = utcnow()
            db.commit()
            return

        _finalize_job(db, job_id)


def _generate_pending(db: Session, job: Job) -> None:
    pending_ids = db.scalars(
        select(Certificate.id)
        .where(Certificate.job_id == job.id, Certificate.status == CertificateStatus.PENDING.value)
        .order_by(Certificate.row_index)
    ).all()

    for cert_id in pending_ids:
        cert = db.get(Certificate, cert_id)
        relative_path = f"{job.id}/{cert.id}.pdf"
        try:
            render_certificate(
                CertificateData(
                    recipient_name=cert.recipient_name,
                    course_name=job.course_name,
                    issuer_name=job.issuer_name,
                    certificate_title=job.certificate_title,
                    issue_date=job.issue_date,
                    certificate_id=cert.id,
                ),
                settings.storage_dir / relative_path,
            )
        except Exception as exc:  # isolate: one bad certificate must not stop the job
            cert.status = CertificateStatus.FAILED.value
            cert.error = _describe_error(exc)
            logger.warning("Certificate %s (job %s row %s) failed: %s", cert.id, job.id, cert.row_index, cert.error)
        else:
            cert.status = CertificateStatus.SUCCEEDED.value
            cert.file_path = relative_path
            cert.generated_at = utcnow()
        db.commit()


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, CertificateRenderError):
        return str(exc)
    return f"Unexpected error: {type(exc).__name__}: {exc}"[:500]


def _finalize_job(db: Session, job_id: str) -> None:
    job = db.get(Job, job_id)
    counts = get_counts(db, job)
    if counts.failed == 0:
        job.status = JobStatus.COMPLETED.value
    elif counts.succeeded == 0:
        job.status = JobStatus.FAILED.value
    else:
        job.status = JobStatus.COMPLETED_WITH_ERRORS.value
    job.completed_at = utcnow()
    db.commit()


# ----------------------------------------------------------------- queries

def get_counts(db: Session, job: Job) -> Counts:
    rows = db.execute(
        select(Certificate.status, func.count())
        .where(Certificate.job_id == job.id)
        .group_by(Certificate.status)
    ).all()
    by_status = {status: n for status, n in rows}
    return Counts(
        total=job.total,
        pending=by_status.get(CertificateStatus.PENDING.value, 0),
        succeeded=by_status.get(CertificateStatus.SUCCEEDED.value, 0),
        failed=by_status.get(CertificateStatus.FAILED.value, 0),
    )


def build_job_detail(db: Session, job: Job) -> JobDetail:
    counts = get_counts(db, job)
    done = counts.succeeded + counts.failed
    progress = round(100 * done / counts.total, 1) if counts.total else 100.0

    failed_rows = db.scalars(
        select(Certificate)
        .where(Certificate.job_id == job.id, Certificate.status == CertificateStatus.FAILED.value)
        .order_by(Certificate.row_index)
        .limit(FAILURE_PREVIEW_LIMIT + 1)
    ).all()

    return JobDetail(
        id=job.id,
        status=JobStatus(job.status),
        certificate_title=job.certificate_title,
        course_name=job.course_name,
        issuer_name=job.issuer_name,
        issue_date=job.issue_date,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error=job.error,
        counts=counts,
        progress_percent=progress,
        failures=[
            FailureOut(row_index=c.row_index, recipient=c.raw_data, error=c.error or "Unknown error")
            for c in failed_rows[:FAILURE_PREVIEW_LIMIT]
        ],
        failures_truncated=len(failed_rows) > FAILURE_PREVIEW_LIMIT,
    )

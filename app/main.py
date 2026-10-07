"""HTTP API (FastAPI)."""
import logging
import os
import re
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Query, Response

from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app import services
from app.config import settings
from app.database import Base, engine, get_db
from app.models import TERMINAL_JOB_STATUSES, Certificate, CertificateStatus, Job
from app.schemas import CertificateOut, CertificatePage, JobCreate, JobDetail

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)  # fine for this scope; a real project would use Alembic
    yield


app = FastAPI(
    title="Bulk Certificate Generator",
    description="Submit a list of recipients, track the job, download the generated PDF certificates.",
    version="1.0.0",
    lifespan=lifespan,
)
router = APIRouter(prefix="/api/v1")


def _get_job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _slug(text: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")[:50] or "certificate"


@router.post("/jobs", response_model=JobDetail, status_code=202, summary="Create a bulk generation job")
def create_job(
    payload: JobCreate,
    background_tasks: BackgroundTasks,
    response: Response,
    db: Session = Depends(get_db),
):
    """Accepts the whole recipient list at once and returns immediately (202).

    Generation happens in the background; poll `GET /jobs/{id}` for progress.
    Recipients that fail validation are reported in the job's `failures`.
    """
    job, needs_processing = services.create_job(db, payload)
    if needs_processing:
        background_tasks.add_task(services.process_job, job.id)
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    return services.build_job_detail(db, job)


@router.get("/jobs/{job_id}", response_model=JobDetail, summary="Job status and progress")
def get_job(job_id: str, db: Session = Depends(get_db)):
    return services.build_job_detail(db, _get_job_or_404(db, job_id))


@router.get(
    "/jobs/{job_id}/certificates",
    response_model=CertificatePage,
    summary="List a job's certificates (paginated, filterable by status)",
)
def list_certificates(
    job_id: str,
    status_filter: CertificateStatus | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    _get_job_or_404(db, job_id)
    query = select(Certificate).where(Certificate.job_id == job_id)
    if status_filter is not None:
        query = query.where(Certificate.status == status_filter.value)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(Certificate.row_index).limit(limit).offset(offset)).all()
    return CertificatePage(
        total=total,
        limit=limit,
        offset=offset,
        items=[CertificateOut.model_validate(c) for c in items],
    )


@router.get("/certificates/{certificate_id}/download", summary="Download one certificate (PDF)")
def download_certificate(certificate_id: str, db: Session = Depends(get_db)):
    cert = db.get(Certificate, certificate_id)
    if cert is None:
        raise HTTPException(status_code=404, detail="Certificate not found")
    if cert.status == CertificateStatus.PENDING.value:
        raise HTTPException(status_code=409, detail="Certificate has not been generated yet")
    if cert.status == CertificateStatus.FAILED.value:
        raise HTTPException(status_code=409, detail=f"Certificate generation failed: {cert.error}")
    path = settings.storage_dir / cert.file_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Certificate file is missing from storage")
    return FileResponse(path, media_type="application/pdf", filename=f"certificate_{_slug(cert.recipient_name)}.pdf")


@router.get("/jobs/{job_id}/download", summary="Download all successful certificates of a finished job (ZIP)")
def download_job_zip(job_id: str, db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    if job.status not in TERMINAL_JOB_STATUSES:
        raise HTTPException(status_code=409, detail="Job is still running; try again when it has finished")

    certs = db.scalars(
        select(Certificate)
        .where(Certificate.job_id == job_id, Certificate.status == CertificateStatus.SUCCEEDED.value)
        .order_by(Certificate.row_index)
    ).all()
    if not certs:
        raise HTTPException(status_code=404, detail="This job has no successfully generated certificates")

    tmp = tempfile.NamedTemporaryFile(prefix="certificates-", suffix=".zip", delete=False)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
        for cert in certs:
            path = settings.storage_dir / cert.file_path
            if not path.is_file():
                logger.warning("Certificate %s file missing, skipped in ZIP", cert.id)
                continue
            archive.write(path, arcname=f"{cert.row_index:05d}_{_slug(cert.recipient_name)}.pdf")
    tmp.close()
    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=f"certificates_{job_id}.zip",
        background=BackgroundTask(os.unlink, tmp.name),  # delete the temp file after sending
    )


STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def index():
    """Small single-page UI for submitting a batch and downloading results."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/", include_in_schema=False)
def index():
    """Simple web UI for the API."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}


app.include_router(router)

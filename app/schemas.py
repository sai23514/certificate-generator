"""Request/response models and per-recipient validation."""
import unicodedata
from datetime import date, datetime, timezone
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    computed_field,
    field_validator,
)

from app.certificate import ensure_renderable
from app.config import settings
from app.models import CertificateStatus, JobStatus, TERMINAL_JOB_STATUSES


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes; everything we store is UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


UTCDateTime = Annotated[datetime, AfterValidator(_as_utc)]


# ---------------------------------------------------------------- requests

class RecipientIn(BaseModel):
    """One recipient. Validated individually so one bad row never rejects the whole request."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    email: EmailStr | None = None

    @field_validator("name")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        if any(unicodedata.category(ch).startswith("C") for ch in value):
            raise ValueError("name must not contain control characters")
        return value

    @field_validator("email", mode="before")
    @classmethod
    def blank_email_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class JobCreate(BaseModel):
    """Whole-request validation. Problems here are the client's fault -> HTTP 422, nothing stored."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "course_name": "Advanced Python Workshop",
                    "issuer_name": "Acme Academy",
                    "certificate_title": "Certificate of Completion",
                    "issue_date": "2026-03-12",
                    "recipients": [
                        {"name": "Asha Rao", "email": "asha@example.com"},
                        {"name": "Ben Carter"},
                    ],
                }
            ]
        },
    )

    course_name: str = Field(min_length=1, max_length=120)
    issuer_name: str = Field(min_length=1, max_length=100)
    certificate_title: str = Field(default="Certificate of Completion", min_length=1, max_length=80)
    issue_date: date | None = None  # defaults to today (UTC)
    # Items are deliberately untyped here: each one is validated separately (see RecipientIn).
    recipients: list[Any] = Field(min_length=1, max_length=settings.max_recipients)

    @field_validator("course_name", "issuer_name", "certificate_title")
    @classmethod
    def renderable(cls, value: str, info) -> str:
        return ensure_renderable(value, info.field_name)


# --------------------------------------------------------------- responses

class CertificateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    row_index: int
    recipient_name: str | None
    recipient_email: str | None
    status: CertificateStatus
    error: str | None
    generated_at: UTCDateTime | None

    @computed_field
    @property
    def download_url(self) -> str | None:
        if self.status == CertificateStatus.SUCCEEDED:
            return f"/api/v1/certificates/{self.id}/download"
        return None


class CertificatePage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CertificateOut]


class Counts(BaseModel):
    total: int
    pending: int
    succeeded: int
    failed: int


class FailureOut(BaseModel):
    row_index: int
    recipient: Any  # the recipient exactly as submitted
    error: str


class JobDetail(BaseModel):
    id: str
    status: JobStatus
    certificate_title: str
    course_name: str
    issuer_name: str
    issue_date: date
    created_at: UTCDateTime
    started_at: UTCDateTime | None
    completed_at: UTCDateTime | None
    error: str | None
    counts: Counts
    progress_percent: float
    failures: list[FailureOut]  # first N failures; use the certificates endpoint for the rest
    failures_truncated: bool

    @computed_field
    @property
    def links(self) -> dict[str, str]:
        base = f"/api/v1/jobs/{self.id}"
        links = {"self": base, "certificates": f"{base}/certificates"}
        if self.status.value in TERMINAL_JOB_STATUSES and self.counts.succeeded:
            links["download_all"] = f"{base}/download"
        return links

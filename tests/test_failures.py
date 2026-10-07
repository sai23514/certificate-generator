"""One failing certificate must not take the rest of the job down."""
from sqlalchemy import select

from app import services
from app.certificate import render_certificate as real_render
from app.database import SessionLocal
from app.models import Job
from tests.helpers import create_job_without_processing, make_payload, submit


def _recipients(*names):
    return [{"name": n} for n in names]


def test_unrenderable_name_fails_only_that_certificate(client):
    payload = make_payload(1, recipients=_recipients("Asha Rao", "आशा राव", "Ben Carter"))
    job = submit(client, payload)

    assert job["status"] == "completed_with_errors"
    assert job["counts"] == {"total": 3, "pending": 0, "succeeded": 2, "failed": 1}
    assert job["failures"][0]["row_index"] == 2
    assert "cannot render" in job["failures"][0]["error"]


def test_unexpected_error_in_one_certificate_is_isolated(client, monkeypatch):
    def flaky_render(data, dest):
        if data.recipient_name == "Chen Wei":
            raise RuntimeError("disk exploded")
        return real_render(data, dest)

    monkeypatch.setattr(services, "render_certificate", flaky_render)
    job = submit(client, make_payload(4))  # Asha, Ben, Chen, Dara

    assert job["status"] == "completed_with_errors"
    assert job["counts"]["succeeded"] == 3 and job["counts"]["failed"] == 1
    failure = job["failures"][0]
    assert failure["row_index"] == 3
    assert "RuntimeError: disk exploded" in failure["error"]

    # the good ones are all retrievable, the failed one is not
    items = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"]
    for item in items:
        response = client.get(f"/api/v1/certificates/{item['id']}/download")
        assert response.status_code == (409 if item["status"] == "failed" else 200)


def test_job_where_every_certificate_fails_is_marked_failed(client, monkeypatch):
    def always_fails(data, dest):
        raise RuntimeError("boom")

    monkeypatch.setattr(services, "render_certificate", always_fails)
    job = submit(client, make_payload(3))

    assert job["status"] == "failed"
    assert job["counts"] == {"total": 3, "pending": 0, "succeeded": 0, "failed": 3}
    assert "download_all" not in job["links"]


def test_processing_is_idempotent(client, monkeypatch):
    job = submit(client, make_payload(2))
    calls = []
    monkeypatch.setattr(services, "render_certificate", lambda *a: calls.append(a))

    services.process_job(job["id"])  # job already finished -> nothing happens

    assert calls == []


def test_retry_only_processes_certificates_that_are_still_pending(client, monkeypatch):
    # Simulate a worker that died half-way: row 1 is already done, rows 2-3 are still pending.
    from app.models import Certificate

    job_id = create_job_without_processing(make_payload(3))
    with SessionLocal() as db:
        first = db.scalars(select(Certificate).where(Certificate.row_index == 1)).one()
        first.status = "succeeded"
        db.commit()

    seen = []

    def spy(data, dest):
        seen.append(data.recipient_name)
        return real_render(data, dest)

    monkeypatch.setattr(services, "render_certificate", spy)
    services.process_job(job_id)

    assert seen == ["Ben Carter", "Chen Wei"]


def test_crash_of_the_job_itself_marks_it_failed(client, monkeypatch):
    def explode(db, job):
        raise RuntimeError("database went away")

    monkeypatch.setattr(services, "_generate_pending", explode)
    job_id = create_job_without_processing(make_payload(2))
    services.process_job(job_id)

    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.status == "failed"
        assert "database went away" in job.error
        assert job.completed_at is not None

"""Job status / progress reporting."""
from app import services
from app.certificate import render_certificate as real_render
from app.database import SessionLocal
from app.models import Job
from tests.helpers import create_job_without_processing, make_payload, submit


def test_job_is_pending_with_zero_progress_before_processing(client):
    job_id = create_job_without_processing(make_payload(4))
    job = client.get(f"/api/v1/jobs/{job_id}").json()

    assert job["status"] == "pending"
    assert job["progress_percent"] == 0.0
    assert job["counts"] == {"total": 4, "pending": 4, "succeeded": 0, "failed": 0}
    assert job["started_at"] is None and job["completed_at"] is None
    assert "download_all" not in job["links"]


def test_progress_is_visible_while_the_job_is_running(client, monkeypatch):
    job_id = create_job_without_processing(make_payload(5))
    snapshots = []

    def spy(data, dest):
        # Look at the job from "outside" (a separate session) right before each certificate.
        with SessionLocal() as db:
            detail = services.build_job_detail(db, db.get(Job, job_id))
        snapshots.append((detail.status.value, detail.counts.succeeded, detail.progress_percent))
        return real_render(data, dest)

    monkeypatch.setattr(services, "render_certificate", spy)
    services.process_job(job_id)

    assert snapshots == [
        ("processing", 0, 0.0),
        ("processing", 1, 20.0),
        ("processing", 2, 40.0),
        ("processing", 3, 60.0),
        ("processing", 4, 80.0),
    ]
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "completed"


def test_failed_certificates_count_towards_progress(client):
    job = submit(client, make_payload(1, recipients=[{"name": "Good"}, {"name": ""}, {"name": "Also Good"}]))

    assert job["counts"] == {"total": 3, "pending": 0, "succeeded": 2, "failed": 1}
    assert job["progress_percent"] == 100.0


def test_failure_list_is_capped_but_complete_list_is_available(client, monkeypatch):
    monkeypatch.setattr(services, "FAILURE_PREVIEW_LIMIT", 2)
    recipients = [{"name": ""} for _ in range(5)] + [{"name": "Valid"}]
    job = submit(client, make_payload(1, recipients=recipients))

    assert job["counts"]["failed"] == 5
    assert len(job["failures"]) == 2 and job["failures_truncated"] is True

    page = client.get(f"/api/v1/jobs/{job['id']}/certificates", params={"status": "failed"}).json()
    assert page["total"] == 5

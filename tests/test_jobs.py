"""Creating a generation job."""
from datetime import datetime, timezone

from tests.helpers import make_payload, submit


def test_create_job_is_accepted_and_returns_a_job_id(client):
    response = client.post("/api/v1/jobs", json=make_payload(3))

    assert response.status_code == 202
    body = response.json()
    assert body["id"]
    assert response.headers["location"] == f"/api/v1/jobs/{body['id']}"
    # The response describes the job as accepted, before the background work has run.
    assert body["status"] == "pending"
    assert body["counts"] == {"total": 3, "pending": 3, "succeeded": 0, "failed": 0}


def test_job_is_processed_in_the_background(client):
    job = submit(client, make_payload(3))

    assert job["status"] == "completed"
    assert job["counts"] == {"total": 3, "pending": 0, "succeeded": 3, "failed": 0}
    assert job["progress_percent"] == 100.0
    assert job["started_at"] and job["completed_at"]
    assert "download_all" in job["links"]


def test_job_stores_certificate_info(client):
    job = submit(client, make_payload(1, certificate_title="Award of Excellence"))

    assert job["course_name"] == "Advanced Python Workshop"
    assert job["issuer_name"] == "Acme Academy"
    assert job["certificate_title"] == "Award of Excellence"
    assert job["issue_date"] == "2026-03-12"


def test_issue_date_defaults_to_today(client):
    payload = make_payload(1)
    del payload["issue_date"]
    job = submit(client, payload)

    assert job["issue_date"] == datetime.now(timezone.utc).date().isoformat()
    assert job["certificate_title"] == "Certificate of Completion"


def test_unknown_job_returns_404(client):
    assert client.get("/api/v1/jobs/does-not-exist").status_code == 404
    assert client.get("/api/v1/jobs/does-not-exist/certificates").status_code == 404

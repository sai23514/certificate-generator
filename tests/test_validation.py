"""Input validation: request-level errors (422) vs recipient-level errors (reported in the job)."""
import pytest

from tests.helpers import make_payload, submit


# ---- request-level: the whole request is rejected, nothing is stored

def _without(key):
    payload = make_payload(2)
    del payload[key]
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(make_payload(0), id="empty-recipient-list"),
        pytest.param(_without("recipients"), id="missing-recipients"),
        pytest.param(_without("course_name"), id="missing-course-name"),
        pytest.param(_without("issuer_name"), id="missing-issuer-name"),
        pytest.param(make_payload(2, course_name="   "), id="blank-course-name"),
        pytest.param(make_payload(2, recipients="Asha Rao"), id="recipients-not-a-list"),
        pytest.param(make_payload(51), id="too-many-recipients"),
        pytest.param(make_payload(2, issue_date="12/03/2026"), id="bad-date"),
        pytest.param(make_payload(2, course_name="पायथन कोर्स"), id="unrenderable-course-name"),
    ],
)
def test_invalid_request_is_rejected_with_422(client, payload):
    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 422


def test_rejected_request_creates_no_job(client):
    client.post("/api/v1/jobs", json=make_payload(0))
    from app.database import SessionLocal
    from app.models import Job
    from sqlalchemy import func, select

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0


# ---- recipient-level: bad rows are reported, good rows still succeed

def test_invalid_recipients_are_reported_and_valid_ones_still_generated(client):
    payload = make_payload(
        1,
        recipients=[
            {"name": "Valid One", "email": "one@example.com"},  # 1 ok
            {"name": "", "email": "blank@example.com"},  # 2 blank name
            {"email": "noname@example.com"},  # 3 missing name
            {"name": "Bad Email", "email": "not-an-email"},  # 4 invalid email
            "just a string",  # 5 not an object
            {"name": "Valid Two"},  # 6 ok, email optional
        ],
    )
    job = submit(client, payload)

    assert job["status"] == "completed_with_errors"
    assert job["counts"] == {"total": 6, "pending": 0, "succeeded": 2, "failed": 4}
    assert job["progress_percent"] == 100.0

    failures = {f["row_index"]: f for f in job["failures"]}
    assert sorted(failures) == [2, 3, 4, 5]
    assert "name" in failures[2]["error"]
    assert "name" in failures[3]["error"]
    assert "email" in failures[4]["error"]
    assert "object" in failures[5]["error"]
    assert failures[4]["recipient"] == {"name": "Bad Email", "email": "not-an-email"}  # echoed as submitted


def test_all_invalid_recipients_finish_immediately_as_failed(client):
    payload = make_payload(1, recipients=[{"name": ""}, {"email": "x@example.com"}])
    response = client.post("/api/v1/jobs", json=payload)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "failed"  # nothing to process, so no background step is needed
    assert body["counts"] == {"total": 2, "pending": 0, "succeeded": 0, "failed": 2}
    assert body["completed_at"] is not None


@pytest.mark.parametrize(
    "name",
    ["Bad\x00Name", "New\nLine", "x" * 101],
    ids=["control-char", "newline", "too-long"],
)
def test_bad_names_are_rejected_per_recipient(client, name):
    job = submit(client, make_payload(1, recipients=[{"name": name}, {"name": "Good Person"}]))

    assert job["counts"]["succeeded"] == 1
    assert [f["row_index"] for f in job["failures"]] == [1]


def test_names_are_trimmed_and_blank_email_is_ignored(client):
    job = submit(client, make_payload(1, recipients=[{"name": "  Asha Rao  ", "email": "  "}]))
    items = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"]

    assert job["status"] == "completed"
    assert items[0]["recipient_name"] == "Asha Rao"
    assert items[0]["recipient_email"] is None

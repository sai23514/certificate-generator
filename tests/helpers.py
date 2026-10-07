from app import services
from app.database import SessionLocal
from app.schemas import JobCreate

PEOPLE = ["Asha Rao", "Ben Carter", "Chen Wei", "Dara Singh", "Elena Petrova", "Farid Khan", "Grace Lee", "Hiro Tanaka"]


def make_payload(count: int = 3, **overrides) -> dict:
    payload = {
        "course_name": "Advanced Python Workshop",
        "issuer_name": "Acme Academy",
        "issue_date": "2026-03-12",
        "recipients": [
            {"name": PEOPLE[i % len(PEOPLE)] + (f" {i // len(PEOPLE) + 1}" if i >= len(PEOPLE) else ""),
             "email": f"person{i}@example.com"}
            for i in range(count)
        ],
    }
    payload.update(overrides)
    return payload


def create_job_without_processing(payload: dict) -> str:
    """Create a job directly through the service layer; the background step is NOT run."""
    with SessionLocal() as db:
        job, _ = services.create_job(db, JobCreate(**payload))
        return job.id


def submit(client, payload: dict) -> dict:
    """POST a job and return the final job status (background work has finished by then)."""
    response = client.post("/api/v1/jobs", json=payload)
    assert response.status_code == 202, response.text
    return client.get(f"/api/v1/jobs/{response.json()['id']}").json()

# Bulk Certificate Generator

A backend API that takes a list of recipients in **one request**, generates a PDF certificate for each
valid recipient from a single predefined template, tracks progress, and lets the client download the results.

**Stack:** Python 3.10+, FastAPI, SQLAlchemy 2 (SQLite by default, any SQL database via `DATABASE_URL`), ReportLab (PDF), pytest.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt  # runtime + test dependencies
```

## Run

```bash
uvicorn app.main:app --reload
```

* **Web UI: http://127.0.0.1:8000/** (paste recipients, watch progress, download PDFs / ZIP)
* API: http://127.0.0.1:8000/api/v1 &nbsp;|&nbsp; interactive docs (Swagger): http://127.0.0.1:8000/docs
* Tables are created on startup. Data goes to `./certificates.db`, PDFs to `./storage/<job_id>/<certificate_id>.pdf`.

Optional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./certificates.db` | Any SQLAlchemy URL, e.g. `postgresql+psycopg://user:pw@host/db` (install the driver) |
| `STORAGE_DIR` | `./storage` | Where generated PDFs are written |
| `MAX_RECIPIENTS_PER_JOB` | `5000` | Max recipients in one request (more -> HTTP 422) |

## Tests

```bash
pytest
```

Tests use a temporary SQLite database and storage folder, so they never touch real data.
They cover job creation, validation, certificate generation, progress/status, individual failures, retrieval and the web UI page.

> If `pytest` crashes with a `pytest_asyncio ... 'Package' object has no attribute 'obj'` error, an old global
> `pytest-asyncio` plugin is installed. Use a fresh virtual environment (see Setup) or run `pytest -p no:asyncio`.

## API

### 1. Submit a job - `POST /api/v1/jobs`

```bash
curl -X POST http://127.0.0.1:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "course_name": "Advanced Python Workshop",
    "issuer_name": "Acme Academy",
    "certificate_title": "Certificate of Completion",
    "issue_date": "2026-03-12",
    "recipients": [
      {"name": "Asha Rao", "email": "asha@example.com"},
      {"name": "Ben Carter"},
      {"name": ""}
    ]
  }'
```

`certificate_title` (default "Certificate of Completion") and `issue_date` (default today, UTC) are optional.
Each recipient needs a `name` (1-100 chars); `email` is optional but must be valid if present.

Returns **202 Accepted** immediately, with a `Location` header and the job:

```json
{
  "id": "5c0f...",
  "status": "pending",
  "counts": {"total": 3, "pending": 2, "succeeded": 0, "failed": 1},
  "progress_percent": 33.3,
  "failures": [{"row_index": 3, "recipient": {"name": ""}, "error": "name: String should have at least 1 character"}],
  "links": {"self": "/api/v1/jobs/5c0f...", "certificates": "/api/v1/jobs/5c0f.../certificates"}
}
```

### 2. Check progress - `GET /api/v1/jobs/{job_id}`

Poll this. `status` is one of:

| Status | Meaning |
|---|---|
| `pending` | accepted, waiting for the worker |
| `processing` | certificates are being generated (`progress_percent` rises) |
| `completed` | every recipient succeeded |
| `completed_with_errors` | some succeeded, some failed - see `failures` |
| `failed` | nothing succeeded (or the job itself crashed - see `error`) |

`counts` gives total / pending / succeeded / failed. `failures` lists the first 50 failed rows with the
original input and the reason (`failures_truncated` tells you if there are more).

### 3. List certificates - `GET /api/v1/jobs/{job_id}/certificates`

Paginated (`limit` 1-500, `offset`) and filterable with `?status=succeeded|failed|pending`.
Each item has `id`, `row_index`, `recipient_name`, `status`, `error` and, when succeeded, a `download_url`.

### 4. Download

* One certificate: `GET /api/v1/certificates/{certificate_id}/download` -> PDF
* Everything that succeeded: `GET /api/v1/jobs/{job_id}/download` -> ZIP (only once the job has finished)

```bash
curl -OJ http://127.0.0.1:8000/api/v1/jobs/<job_id>/download
```

Errors: `404` unknown id / nothing to download, `409` certificate not ready, failed, or job still running, `422` invalid request.

## Design decisions

**Background processing, not synchronous.** A request may contain thousands of recipients; generating them inline
would hold the HTTP connection open for seconds-to-minutes and risk client/proxy timeouts. So `POST /jobs`
validates and stores the request, returns 202, and FastAPI's `BackgroundTasks` runs `process_job` in a worker
thread. The client polls for progress. (Measured locally: 2,000 certificates in about 5 seconds.)

**Why `BackgroundTasks` and not Celery/RQ.** It keeps the project runnable with zero infrastructure, which fits the scope.
The trade-off is durability: if the server process dies mid-job, the job stays `processing`. The processor is written
so a proper queue can replace it without touching the rest: `process_job(job_id)` is the only entry point, it only
touches `pending` rows (so retrying is safe), and the job state lives entirely in the database.

**Two levels of validation.**
* *Request level* (missing course name, empty list, more than the max recipients, invalid date) is the client's mistake:
  the whole request is rejected with 422 and nothing is stored.
* *Recipient level* (blank name, bad email, non-object entry) must not sink the other recipients. These rows are stored
  as already-`failed` with the reason, so they show up in the job's `failures`. If every row is invalid the job is `failed` immediately and no background work is scheduled.

**Failure isolation.** Each certificate is rendered inside its own `try/except` and committed on its own. A failure is
recorded on that row and the loop continues. Per-row commits also make progress visible live. PDFs are written to a
temp file and renamed, so a crash never leaves a half-written certificate.

**Progress is computed, not stored.** Counts come from `GROUP BY status` on the certificates table, so they cannot drift
out of sync with the real rows. The job's final status is derived from those counts when processing ends.

**Data model.** `jobs` (shared certificate info, status, timestamps) and `certificates` (one row per input recipient:
`row_index`, the raw input as JSON, status, error, relative file path). Certificate files are named by UUID, never by
recipient name, so user input never becomes part of a file path.

**Template.** One landscape A4 template drawn with ReportLab (`app/certificate.py`): title, recipient name (auto-shrinks to
fit), course name (wraps up to 3 lines), issue date, issuer, certificate ID. The renderer is independent of the DB and HTTP.

## Known limitations / what I'd do next

* ReportLab's built-in fonts cover Latin (WinAnsi) text only. Other scripts (e.g. Devanagari) are rejected with a clear
  per-certificate error instead of rendering as black boxes. Fix: register a Unicode TTF font (and a shaping library for Indic scripts).
* No authentication, rate limiting or file cleanup/retention policy.
* Generation is sequential inside one worker. For much larger volumes: a real queue (Celery/RQ) with several workers,
  and object storage (S3) instead of local disk.
* Tables are created with `create_all`; a real project would use Alembic migrations.
* No duplicate detection: the same person listed twice gets two certificates.

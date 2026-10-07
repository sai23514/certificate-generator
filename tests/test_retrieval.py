"""Retrieving generated certificates."""
import io
import zipfile

from app.config import settings
from tests.helpers import create_job_without_processing, make_payload, submit


def _items(client, job_id, **params):
    return client.get(f"/api/v1/jobs/{job_id}/certificates", params=params).json()


def test_download_single_certificate_as_pdf(client):
    job = submit(client, make_payload(2))
    item = _items(client, job["id"])["items"][0]

    assert item["download_url"] == f"/api/v1/certificates/{item['id']}/download"
    response = client.get(item["download_url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "certificate_Asha_Rao.pdf" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")


def test_download_unknown_certificate_returns_404(client):
    assert client.get("/api/v1/certificates/nope/download").status_code == 404


def test_certificate_not_ready_returns_409(client):
    job_id = create_job_without_processing(make_payload(1))
    item = _items(client, job_id)["items"][0]

    response = client.get(f"/api/v1/certificates/{item['id']}/download")
    assert response.status_code == 409
    assert item["download_url"] is None


def test_failed_certificate_cannot_be_downloaded(client):
    job = submit(client, make_payload(1, recipients=[{"name": ""}, {"name": "Good"}]))
    failed = _items(client, job["id"], status="failed")["items"][0]

    response = client.get(f"/api/v1/certificates/{failed['id']}/download")
    assert response.status_code == 409
    assert "failed" in response.json()["detail"]


def test_missing_file_on_disk_returns_404(client):
    job = submit(client, make_payload(1))
    item = _items(client, job["id"])["items"][0]
    (settings.storage_dir / job["id"] / f"{item['id']}.pdf").unlink()

    assert client.get(item["download_url"]).status_code == 404


def test_download_all_as_zip_contains_only_successful_certificates(client):
    job = submit(client, make_payload(1, recipients=[{"name": "Asha Rao"}, {"name": ""}, {"name": "Ben Carter"}]))
    response = client.get(f"/api/v1/jobs/{job['id']}/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["00001_Asha_Rao.pdf", "00003_Ben_Carter.pdf"]
        assert all(archive.read(n).startswith(b"%PDF") for n in archive.namelist())


def test_zip_is_not_available_while_job_is_unfinished(client):
    job_id = create_job_without_processing(make_payload(2))
    assert client.get(f"/api/v1/jobs/{job_id}/download").status_code == 409


def test_zip_returns_404_when_nothing_succeeded(client):
    job = submit(client, make_payload(1, recipients=[{"name": ""}]))
    assert client.get(f"/api/v1/jobs/{job['id']}/download").status_code == 404


def test_certificate_listing_pagination_and_status_filter(client):
    job = submit(client, make_payload(1, recipients=[{"name": "A One"}, {"name": ""}, {"name": "C Three"},
                                                      {"name": "D Four"}, {"name": ""}]))

    everything = _items(client, job["id"])
    assert everything["total"] == 5 and [i["row_index"] for i in everything["items"]] == [1, 2, 3, 4, 5]

    page2 = _items(client, job["id"], limit=2, offset=2)
    assert [i["row_index"] for i in page2["items"]] == [3, 4] and page2["total"] == 5

    succeeded = _items(client, job["id"], status="succeeded")
    assert succeeded["total"] == 3
    failed = _items(client, job["id"], status="failed")
    assert [i["row_index"] for i in failed["items"]] == [2, 5]
    assert all(i["error"] for i in failed["items"])

    assert client.get(f"/api/v1/jobs/{job['id']}/certificates", params={"status": "bogus"}).status_code == 422
    assert client.get(f"/api/v1/jobs/{job['id']}/certificates", params={"limit": 0}).status_code == 422

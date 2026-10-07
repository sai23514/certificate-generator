"""Certificate generation (renderer + end-to-end files)."""
from datetime import date

import pytest
from pypdf import PdfReader

from app.certificate import CertificateData, CertificateRenderError, render_certificate
from app.config import settings
from tests.helpers import make_payload, submit


def _data(**overrides) -> CertificateData:
    values = dict(
        recipient_name="Asha Rao",
        course_name="Advanced Python Workshop",
        issuer_name="Acme Academy",
        certificate_title="Certificate of Completion",
        issue_date=date(2026, 3, 12),
        certificate_id="abc-123",
    )
    values.update(overrides)
    return CertificateData(**values)


def test_rendered_pdf_contains_recipient_specific_information(tmp_path):
    dest = tmp_path / "out.pdf"
    render_certificate(_data(), dest)

    reader = PdfReader(str(dest))
    text = reader.pages[0].extract_text()
    assert len(reader.pages) == 1
    for expected in ["Asha Rao", "Advanced Python Workshop", "Acme Academy", "Certificate of Completion",
                     "12 March 2026", "abc-123"]:
        assert expected in text
    assert not list(tmp_path.glob("*.tmp"))  # no temp file left behind


def test_different_recipients_get_different_certificates(tmp_path):
    render_certificate(_data(recipient_name="Asha Rao"), tmp_path / "a.pdf")
    render_certificate(_data(recipient_name="Ben Carter"), tmp_path / "b.pdf")

    assert "Ben Carter" in PdfReader(str(tmp_path / "b.pdf")).pages[0].extract_text()
    assert "Ben Carter" not in PdfReader(str(tmp_path / "a.pdf")).pages[0].extract_text()


def test_long_name_and_course_still_fit_on_the_page(tmp_path):
    dest = tmp_path / "long.pdf"
    render_certificate(_data(recipient_name="Maximilian " * 9 + "X", course_name="Data Engineering " * 7), dest)
    assert dest.read_bytes().startswith(b"%PDF")


def test_accented_latin_names_are_supported(tmp_path):
    dest = tmp_path / "accent.pdf"
    render_certificate(_data(recipient_name="José Müller"), dest)
    assert dest.exists()


def test_characters_the_font_cannot_draw_raise_a_clear_error(tmp_path):
    with pytest.raises(CertificateRenderError, match="cannot render"):
        render_certificate(_data(recipient_name="आशा राव"), tmp_path / "x.pdf")
    assert not (tmp_path / "x.pdf").exists()


def test_failed_render_leaves_no_partial_file(tmp_path, monkeypatch):
    def broken_draw(data, path):
        path.write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr("app.certificate._draw", broken_draw)
    with pytest.raises(OSError):
        render_certificate(_data(), tmp_path / "x.pdf")
    assert list(tmp_path.iterdir()) == []


def test_job_generates_one_pdf_per_valid_recipient(client):
    job = submit(client, make_payload(5))
    items = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"]

    assert len(items) == 5
    for item in items:
        assert item["status"] == "succeeded"
        stored = list((settings.storage_dir / job["id"]).glob(f"{item['id']}.pdf"))
        assert len(stored) == 1 and stored[0].read_bytes().startswith(b"%PDF")

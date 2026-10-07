"""Certificate rendering: one fixed template, drawn to PDF with reportlab.

The renderer knows nothing about the database or HTTP; it takes plain data and
writes a file. That keeps it easy to test and easy to swap for another format.
"""
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)
# reportlab's built-in fonts only cover WinAnsi (Latin-1-ish). Text outside it would
# render as black boxes, so we reject it up front with a clear message instead.
FONT_ENCODING = "cp1252"

NAVY = HexColor("#1f3a5f")
GOLD = HexColor("#b8964e")
GREY = HexColor("#666666")


class CertificateRenderError(Exception):
    """A single certificate could not be rendered (bad data, not a system fault)."""


@dataclass(frozen=True)
class CertificateData:
    recipient_name: str
    course_name: str
    issuer_name: str
    certificate_title: str
    issue_date: date
    certificate_id: str


def ensure_renderable(text: str, label: str) -> str:
    """Raise ValueError if `text` has characters the template font cannot draw."""
    try:
        text.encode(FONT_ENCODING)
    except UnicodeEncodeError:
        bad = "".join(sorted({ch for ch in text if not _encodable(ch)}))
        raise ValueError(f"{label} contains characters the certificate font cannot render: {bad}") from None
    return text


def _encodable(ch: str) -> bool:
    try:
        ch.encode(FONT_ENCODING)
        return True
    except UnicodeEncodeError:
        return False


def fit_font_size(text: str, font: str, max_size: int, max_width: float, min_size: int = 8) -> int:
    size = max_size
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 1
    if stringWidth(text, font, size) > max_width:
        raise CertificateRenderError(f"Text is too long to fit on the certificate: {text[:40]}...")
    return size


def render_certificate(data: CertificateData, dest: Path) -> None:
    """Write the certificate PDF to `dest` atomically (temp file + rename)."""
    try:
        ensure_renderable(data.recipient_name, "Recipient name")
        ensure_renderable(data.course_name, "Course name")
        ensure_renderable(data.issuer_name, "Issuer name")
        ensure_renderable(data.certificate_title, "Certificate title")
    except ValueError as exc:
        raise CertificateRenderError(str(exc)) from None

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        _draw(data, tmp)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _draw(data: CertificateData, path: Path) -> None:
    w, h = PAGE_WIDTH, PAGE_HEIGHT
    cx = w / 2
    c = canvas.Canvas(str(path), pagesize=(w, h), invariant=1)  # invariant => reproducible bytes
    c.setTitle(f"{data.certificate_title} - {data.recipient_name}")
    c.setAuthor(data.issuer_name)

    # Double border
    c.setStrokeColor(NAVY)
    c.setLineWidth(6)
    c.rect(25, 25, w - 50, h - 50)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.5)
    c.rect(38, 38, w - 76, h - 76)

    # Title
    title_font = "Helvetica-Bold"
    c.setFillColor(NAVY)
    c.setFont(title_font, fit_font_size(data.certificate_title, title_font, 36, w - 160))
    c.drawCentredString(cx, h - 120, data.certificate_title)

    # "This is to certify that" + recipient name
    c.setFillColor(GREY)
    c.setFont("Helvetica", 15)
    c.drawCentredString(cx, h - 175, "This is to certify that")

    name_font = "Times-BoldItalic"
    name_size = fit_font_size(data.recipient_name, name_font, 44, w - 160)
    c.setFillColor(NAVY)
    c.setFont(name_font, name_size)
    c.drawCentredString(cx, h - 235, data.recipient_name)
    half = min(stringWidth(data.recipient_name, name_font, name_size) / 2 + 30, (w - 160) / 2)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.line(cx - half, h - 247, cx + half, h - 247)

    # "has successfully completed" + course name (wrapped, max 3 lines)
    c.setFillColor(GREY)
    c.setFont("Helvetica", 15)
    c.drawCentredString(cx, h - 285, "has successfully completed")

    course_font, course_size = "Helvetica-Bold", 22
    lines = simpleSplit(data.course_name, course_font, course_size, w - 200)
    if len(lines) > 3:
        raise CertificateRenderError("Course name is too long to fit on the certificate")
    c.setFillColor(NAVY)
    c.setFont(course_font, course_size)
    y = h - 328
    for line in lines:
        c.drawCentredString(cx, y, line)
        y -= 28

    # Footer: date (left), issuer (right)
    line_y = 105
    c.setStrokeColor(GREY)
    c.setLineWidth(0.8)
    c.line(90, line_y, 290, line_y)
    c.line(w - 290, line_y, w - 90, line_y)

    c.setFillColor(NAVY)
    c.setFont("Helvetica", 13)
    c.drawCentredString(190, line_y + 8, f"{data.issue_date.day} {data.issue_date:%B %Y}")
    issuer_font = "Helvetica"
    c.setFont(issuer_font, fit_font_size(data.issuer_name, issuer_font, 13, 200))
    c.drawCentredString(w - 190, line_y - 18, data.issuer_name)

    c.setFillColor(GREY)
    c.setFont("Helvetica", 10)
    c.drawCentredString(190, line_y - 18, "Date of issue")

    c.setFont("Helvetica", 8)
    c.drawCentredString(cx, 55, f"Certificate ID: {data.certificate_id}")

    c.showPage()
    c.save()

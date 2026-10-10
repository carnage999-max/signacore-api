"""The certificate of completion, appended to a document that has been signed.

A signed PDF shows what was agreed. It does not show who agreed, when, from where, or that the
file in front of you is the one the signature was made against. Until now that evidence existed
only as rows in our database, which means producing it in a dispute meant exporting from an
admin screen and asking somebody to take our word for the export.

These pages carry it with the document instead. Everything on them comes from what was recorded
at the time - the trail in SigningEvent and the fingerprint taken at upload - and nothing is
computed here that could disagree with it.

Deliberately plain. It is read by people who are looking for a specific fact under pressure,
not browsed, so it is a list of labelled values in one column with the events in date order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

import fitz

from utils.fingerprint import format_fingerprint

# Built into every PDF reader, so nothing has to be embedded and nothing can fail to resolve on
# a machine we have never seen.
BODY_FONT = "helv"
BOLD_FONT = "hebo"

LETTER = (612.0, 792.0)
MARGIN = 54.0
INK = (0.05, 0.08, 0.12)
MUTED = (0.38, 0.43, 0.5)
RULE = (0.82, 0.85, 0.89)


@dataclass(frozen=True)
class CertifiedEvent:
    """One line of the trail, already turned into text."""

    label: str
    at: str
    detail: str
    ip_address: str


@dataclass(frozen=True)
class CertifiedSigner:
    name: str
    email: str
    request_id: str
    signed_at: str
    events: list[CertifiedEvent]


@dataclass(frozen=True)
class Certificate:
    document_title: str
    document_id: str
    document_fingerprint: str
    issued_at: str
    signers: list[CertifiedSigner]


def _utc(value) -> str:
    """Times are printed in UTC and say so.

    A timestamp without a zone is the commonest way an audit record becomes useless, and a
    timestamp in the reader's zone is the second commonest.
    """
    if value is None:
        return "-"
    return value.astimezone(UTC).strftime("%d %B %Y at %H:%M:%S UTC")


def build_certificate(document, signing_requests) -> Certificate:
    """Turn what was recorded into what will be printed. No new facts are invented here."""
    signers = []
    for request in signing_requests:
        events = [
            CertifiedEvent(
                label=event.get_event_display(),
                at=_utc(event.at),
                detail=event.detail or "",
                ip_address=event.ip_address or "",
            )
            for event in request.events.all().order_by("at", "created_at")
        ]
        signers.append(
            CertifiedSigner(
                name=request.signer_name or "Not given",
                email=request.signer_email,
                request_id=str(request.id),
                signed_at=_utc(request.signed_at),
                events=events,
            )
        )
    return Certificate(
        document_title=document.title,
        document_id=str(document.id),
        document_fingerprint=document.original_sha256 or "",
        issued_at=_utc(_now()),
        signers=signers,
    )


def _now():
    from django.utils import timezone

    return timezone.now()


class _Sheet:
    """A page being written down, which starts another when it runs out of room."""

    def __init__(self, pdf: fitz.Document, size: tuple[float, float]) -> None:
        self.pdf = pdf
        self.width, self.height = size
        self.page = pdf.new_page(width=self.width, height=self.height)
        self.y = MARGIN

    @property
    def right(self) -> float:
        return self.width - MARGIN

    def room_for(self, height: float) -> None:
        if self.y + height <= self.height - MARGIN:
            return
        self.page = self.pdf.new_page(width=self.width, height=self.height)
        self.y = MARGIN

    def text(self, value: str, *, size: float, font: str = BODY_FONT, colour=INK, gap: float = 4.0) -> None:
        self.room_for(size + gap)
        self.page.insert_text(fitz.Point(MARGIN, self.y + size), value, fontname=font, fontsize=size, color=colour)
        self.y += size + gap

    def wrapped(self, value: str, *, size: float, colour=MUTED, gap: float = 4.0) -> None:
        """Body text that may not fit on one line.

        insert_textbox returns a negative number when the text did not fit, so the height is
        measured first rather than discovered by the text silently vanishing.
        """
        width = self.right - MARGIN
        height = fitz.get_text_length(value, fontname=BODY_FONT, fontsize=size)
        lines = max(1, int(height / width) + 1)
        block = lines * (size + 2)
        self.room_for(block + gap)
        self.page.insert_textbox(
            fitz.Rect(MARGIN, self.y, self.right, self.y + block + size),
            value,
            fontname=BODY_FONT,
            fontsize=size,
            color=colour,
        )
        self.y += block + gap

    def pair(self, label: str, value: str, *, size: float = 9.5) -> None:
        self.room_for(size + 10)
        self.page.insert_text(fitz.Point(MARGIN, self.y + size), label, fontname=BODY_FONT, fontsize=size, color=MUTED)
        self.page.insert_text(
            fitz.Point(MARGIN + 150, self.y + size), value, fontname=BOLD_FONT, fontsize=size, color=INK
        )
        self.y += size + 7

    def rule(self, *, gap: float = 10.0) -> None:
        self.room_for(gap * 2)
        self.y += gap
        self.page.draw_line(fitz.Point(MARGIN, self.y), fitz.Point(self.right, self.y), color=RULE, width=0.6)
        self.y += gap

    def keep_together(self, height: float) -> None:
        """Start a new page unless this much will fit, so a block is not split at its heading."""
        if self.y + height > self.height - MARGIN:
            self.page = self.pdf.new_page(width=self.width, height=self.height)
            self.y = MARGIN

    def space(self, amount: float) -> None:
        self.room_for(amount)
        self.y += amount


SEAL_RADIUS = 33.0
SEAL_INK = (0.06, 0.42, 0.31)
SEAL_WASH = (0.93, 0.97, 0.95)


def _centred(page: fitz.Page, x: float, baseline: float, text: str, *, font: str, size: float, colour) -> None:
    """Place text centred on x, measured rather than boxed.

    insert_textbox silently drops text that does not fit and reports it only through a negative
    return value, which on a certificate means a fact quietly going missing. Measuring the
    string and placing it by its baseline cannot fail that way.
    """
    width = fitz.get_text_length(text, fontname=font, fontsize=size)
    page.insert_text(fitz.Point(x - width / 2, baseline), text, fontname=font, fontsize=size, color=colour)


def _draw_seal(page: fitz.Page, signature_count: int) -> None:
    """A seal, in the place a seal goes on a document that has been completed.

    Drawn from shapes rather than placed as an image, so there is no asset to go missing and
    nothing to go blurry at print size.

    It says only what we watched happen: how many signatures were collected, and that each
    signer confirmed a code sent to their own address. It is not a compliance mark and borrows
    nobody else's. A seal on a legal document implying an accreditation we do not hold would be
    worse than no seal at all.
    """
    centre = fitz.Point(page.rect.width - MARGIN - SEAL_RADIUS, MARGIN + SEAL_RADIUS)
    page.draw_circle(centre, SEAL_RADIUS, color=SEAL_INK, fill=SEAL_WASH, width=1.4)
    page.draw_circle(centre, SEAL_RADIUS - 6, color=SEAL_INK, width=0.5)

    # The tick sits above the middle so the two lines of type below it stay inside the inner
    # ring, which narrows quickly: at fifteen points below centre there is only about forty-five
    # points of width left to write in.
    page.draw_line(
        fitz.Point(centre.x - 10, centre.y - 12),
        fitz.Point(centre.x - 4, centre.y - 6),
        color=SEAL_INK,
        width=2.4,
    )
    page.draw_line(
        fitz.Point(centre.x - 4, centre.y - 6),
        fitz.Point(centre.x + 10, centre.y - 20),
        color=SEAL_INK,
        width=2.4,
    )

    signatures = "1 SIGNATURE" if signature_count == 1 else f"{signature_count} SIGNATURES"
    # Each line has to fit the chord of the inner ring at its own height, which is narrower
    # than the diameter and narrows fast. A test holds both inside it.
    _centred(page, centre.x, centre.y + 4, signatures, font=BOLD_FONT, size=7, colour=SEAL_INK)
    _centred(page, centre.x, centre.y + 14, "EMAIL VERIFIED", font=BODY_FONT, size=5.5, colour=SEAL_INK)


def render_certificate(certificate: Certificate, size: tuple[float, float] = LETTER) -> fitz.Document:
    pdf = fitz.open()
    sheet = _Sheet(pdf, size)

    _draw_seal(sheet.page, len(certificate.signers))
    sheet.text("Certificate of Completion", size=19, font=BOLD_FONT, gap=3)
    sheet.text("Issued by SignaCore", size=9.5, colour=MUTED, gap=2)
    # Clear the seal before ruling off, or the line is drawn straight through it.
    sheet.space(max(0.0, (MARGIN + SEAL_RADIUS * 2 + 8) - sheet.y))
    sheet.rule()

    sheet.text("THE DOCUMENT", size=8.5, font=BOLD_FONT, colour=MUTED, gap=8)
    sheet.pair("Title", certificate.document_title)
    sheet.pair("Document reference", certificate.document_id)
    sheet.pair("Certificate issued", certificate.issued_at)
    sheet.space(4)
    sheet.text("Fingerprint of the document as uploaded (SHA-256)", size=8.5, colour=MUTED, gap=3)
    if certificate.document_fingerprint:
        sheet.wrapped(format_fingerprint(certificate.document_fingerprint), size=8.5, colour=INK)
        sheet.wrapped(
            "Run shasum -a 256 against the original to compare. A document that produces a "
            "different fingerprint is not the document this certificate describes.",
            size=8,
        )
    else:
        sheet.wrapped(
            "Not recorded. This document was uploaded before SignaCore fingerprinted them.",
            size=8.5,
        )

    for signer in certificate.signers:
        sheet.rule()
        # Keep a signer's heading with their details. A page that ends on "THE SIGNER" and
        # carries the name over the fold is the kind of thing somebody reads past in a hurry.
        sheet.keep_together(120)
        sheet.text("THE SIGNER", size=8.5, font=BOLD_FONT, colour=MUTED, gap=8)
        sheet.pair("Name", signer.name)
        sheet.pair("Email address", signer.email)
        sheet.pair("Identity confirmed by", "A one-time code sent to that address")
        sheet.pair("Signed", signer.signed_at)
        sheet.pair("Signing reference", signer.request_id)

        sheet.space(8)
        sheet.text("WHAT HAPPENED", size=8.5, font=BOLD_FONT, colour=MUTED, gap=8)
        if not signer.events:
            sheet.wrapped("No events were recorded for this signer.", size=9)
        for event in signer.events:
            sheet.room_for(34)
            sheet.page.insert_text(
                fitz.Point(MARGIN, sheet.y + 9), event.label, fontname=BOLD_FONT, fontsize=9, color=INK
            )
            sheet.page.insert_text(
                fitz.Point(sheet.right - 190, sheet.y + 9),
                event.at,
                fontname=BODY_FONT,
                fontsize=8.5,
                color=MUTED,
            )
            sheet.y += 13
            footnote = " · ".join(part for part in (event.detail, _from(event.ip_address)) if part)
            if footnote:
                sheet.page.insert_text(
                    fitz.Point(MARGIN, sheet.y + 8), footnote, fontname=BODY_FONT, fontsize=8, color=MUTED
                )
                sheet.y += 12
            sheet.y += 4

    sheet.rule()
    sheet.wrapped(
        "Every time on this certificate is in UTC and was taken by SignaCore's own servers, not "
        "by the signer's device. Addresses are those the request reached us from. The completed "
        "document, the signature images and the values entered are encrypted at rest and "
        "decrypted only to do the work that was asked for.",
        size=8,
    )
    sheet.wrapped(
        "This certificate records what SignaCore observed. Whether an agreement is enforceable "
        "depends on the document, the parties and the law that applies to it.",
        size=8,
    )
    _number_the_pages(pdf)
    return pdf


def _number_the_pages(pdf: fitz.Document) -> None:
    """So a reader can see that none of the certificate is missing.

    Numbered after the fact rather than as each page is written, because the total is not known
    until the last event has been placed.
    """
    total = pdf.page_count
    for index, page in enumerate(pdf, start=1):
        page.insert_text(
            fitz.Point(MARGIN, page.rect.height - MARGIN + 18),
            f"Certificate of completion, page {index} of {total}",
            fontname=BODY_FONT,
            fontsize=7.5,
            color=MUTED,
        )


def _from(ip_address: str) -> str:
    return f"from {ip_address}" if ip_address else ""


def append_certificate(pdf_path: str | Path, certificate: Certificate) -> None:
    """Add the certificate to the end of a completed copy, in place.

    Appended rather than delivered separately so the evidence cannot be parted from the document
    by the time it matters, which is usually months later in somebody else's inbox.
    """
    source = Path(pdf_path)
    # PyMuPDF cannot write over a file it still has open, and an incremental save will not do
    # after pages have been added, so the result is built beside it and moved into place.
    staged = source.with_suffix(".with-certificate.pdf")
    target = fitz.open(source)
    try:
        size = (target[0].rect.width, target[0].rect.height) if target.page_count else LETTER
        pages = render_certificate(certificate, size=size)
        try:
            target.insert_pdf(pages)
        finally:
            pages.close()
        target.save(str(staged), garbage=4, clean=True, deflate=True)
    finally:
        target.close()
    staged.replace(source)

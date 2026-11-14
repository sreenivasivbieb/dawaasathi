"""Test helpers shared by several modules: FakeOCR with canned strip / prescription payloads,
a tiny WAV generator and the per-language doctor line."""

from __future__ import annotations

import io
import wave

from app.adapters.ocr import OCRItem, OCRPayload, OCRResult, OCRSalt
from app.dialogue.templates import load as load_templates


# ---------------------------------------------------------------- fake OCR
def _salt(name: str, amount: float | None, unit: str | None = "mg", per_ml: float | None = None) -> OCRSalt:
    return OCRSalt(name=name, amount=amount, unit=unit, per_ml=per_ml)


FAKE_IMAGES: dict[bytes, OCRPayload] = {
    b"STRIP_AUG": OCRPayload(kind="strip", items=[OCRItem(
        brand="Augmentin 625 Duo", salts=[_salt("Amoxycillin", 500), _salt("Clavulanic Acid", 125)],
        composition_text="Amoxycillin 500 mg + Clavulanic Acid 125 mg", form="tablet", mrp=223.40,
        pack_qty=10, pack_unit="tablets", batch_no="AUG2401", manufacturer="GlaxoSmithKline")]),
    b"STRIP_GLY_SR": OCRPayload(kind="strip", items=[OCRItem(
        brand="Glycomet SR 500", salts=[_salt("Metformin Hydrochloride", 500)],
        composition_text="Metformin Hydrochloride (as SR) 500 mg", form="sustained release tablet",
        release="SR", mrp=40.0, pack_qty=20, pack_unit="tablets", batch_no="GSR1102")]),
    b"STRIP_DOLO_NSQ": OCRPayload(kind="strip", items=[OCRItem(
        brand="Dolo 650", salts=[_salt("Paracetamol", 650)], composition_text="Paracetamol IP 650 mg",
        form="tablet", mrp=33.60, pack_qty=15, pack_unit="tablets", batch_no="DEMO123")]),
    b"STRIP_DOLO": OCRPayload(kind="strip", items=[OCRItem(
        brand="Dolo 650", salts=[_salt("Paracetamol", 650)], composition_text="Paracetamol IP 650 mg",
        form="tablet", mrp=33.60, pack_qty=15, pack_unit="tablets", batch_no="DL9001")]),
    b"STRIP_FDC": OCRPayload(kind="strip", items=[OCRItem(
        brand="Saathi-Demo FDC", salts=[_salt("Aceclofenac", 100), _salt("Paracetamol", 325),
                                        _salt("Rabeprazole Sodium", 20)],
        form="tablet", mrp=95.0, pack_qty=10, pack_unit="tablets", batch_no="FDC001")]),
    b"STRIP_UNREADABLE": OCRPayload(kind="strip", readable=False, items=[]),
    b"RX_GLY": OCRPayload(kind="rx", items=[OCRItem(
        brand="Glycomet 500", form="Tab", directions="1-0-1 after food")]),
    b"RX_AUG": OCRPayload(kind="rx", items=[OCRItem(
        brand="Augmentin 625", form="Tab", directions="1-0-1 x 5 days")]),
    b"RX_TWO": OCRPayload(kind="rx", items=[
        OCRItem(brand="Augmentin 625", form="Tab", directions="1-0-1 x 5 days"),
        OCRItem(brand="Pan 40", form="Tab", directions="1-0-0 before food")]),
}


class FakeOCR:
    """Stands in for OCRReader: the image bytes select a canned payload."""

    provider = "fake"

    def __init__(self, images: dict[bytes, OCRPayload] | None = None):
        self.images = FAKE_IMAGES if images is None else images
        self.calls: list[tuple[str, bytes]] = []

    def _get(self, image: bytes, kind: str) -> OCRResult:
        self.calls.append((kind, image))
        payload = self.images.get(image)
        if payload is None:
            payload = OCRPayload(kind=kind, readable=False)
        return OCRResult(payload.model_copy(deep=True), "fake", 0.0)

    async def read_strip(self, image: bytes) -> OCRResult:
        return self._get(image, "strip")

    async def read_rx(self, image: bytes) -> OCRResult:
        return self._get(image, "rx")


def tiny_wav(seconds: float = 0.2, sample_rate: int = 16000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(int(sample_rate * seconds) * 2))
    return out.getvalue()


def doctor_line(lang: str) -> str:
    return load_templates(lang)["lines"]["doctor"]


# Lines that tests want shown at the end of the run (see conftest.pytest_terminal_summary).
SUMMARY: list[str] = []

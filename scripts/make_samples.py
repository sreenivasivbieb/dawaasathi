"""Draw synthetic strip backs and prescriptions for demos and tests (no real photos, no PII).

  uv run python -m scripts.make_samples        -> samples/*.png
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples"

FONT_DIRS = ["/usr/share/fonts/TTF", "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu"]


def _font(size: int, bold: bool = False, hand: bool = False):
    names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
    if hand:
        names = ["DejaVuSerif-Italic.ttf", "DejaVuSans-Oblique.ttf"] + names
    for d in FONT_DIRS:
        for n in names:
            p = Path(d) / n
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def strip(name: str, brand: str, lines: list[str], mrp: str, batch: str, seed: int = 0) -> Path:
    rnd = random.Random(seed)
    w, h = 1100, 520
    img = Image.new("RGB", (w, h), (196, 200, 206))
    d = ImageDraw.Draw(img)
    # foil sheen + blister bumps
    for x in range(0, w, 6):
        shade = 190 + int(25 * abs(((x / w) * 2) - 1))
        d.line([(x, 0), (x, h)], fill=(shade, shade + 3, shade + 8), width=6)
    for cx in range(120, w, 220):
        for cy in (130, 390):
            d.ellipse([cx - 70, cy - 55, cx + 70, cy + 55], outline=(170, 174, 180), width=3)
    d.text((40, 24), brand, fill=(20, 40, 130), font=_font(40, bold=True))
    y = 92
    for ln in lines:
        d.text((40, y), ln, fill=(25, 25, 25), font=_font(23))
        y += 34
    d.text((40, y + 10), f"B.No. {batch}   Mfg. 03/2026   Exp. 02/2028", fill=(25, 25, 25), font=_font(23))
    d.text((40, y + 46), f"M.R.P. Rs. {mrp} (incl. of all taxes)", fill=(25, 25, 25), font=_font(23))
    d.text((40, h - 46), "SYNTHETIC SAMPLE - Demo Pharma Ltd", fill=(90, 90, 90), font=_font(19))
    img = img.rotate(rnd.uniform(-3, 3), expand=False, fillcolor=(180, 180, 180)).filter(ImageFilter.GaussianBlur(0.6))
    path = OUT / f"{name}.png"
    img.save(path)
    return path


def prescription(name: str, items: list[tuple[str, str]], seed: int = 0) -> Path:
    rnd = random.Random(seed)
    w, h = 900, 1100
    img = Image.new("RGB", (w, h), (252, 250, 240))
    d = ImageDraw.Draw(img)
    d.text((40, 30), "Dr. A. Demo, MBBS (Synthetic Clinic)", fill=(20, 20, 90), font=_font(30, bold=True))
    d.text((40, 75), "Reg. No. XXXX   |   Not a real prescription", fill=(60, 60, 60), font=_font(20))
    d.line([(40, 115), (w - 40, 115)], fill=(20, 20, 90), width=3)
    d.text((40, 140), "Patient: Demo Patient    Age: 45    Date: 12/11/2026", fill=(30, 30, 30), font=_font(22))
    d.text((40, 200), "Rx", fill=(20, 20, 90), font=_font(54, bold=True))
    y = 290
    for med, directions in items:
        d.text((70 + rnd.randint(-4, 4), y), med, fill=(10, 30, 120), font=_font(34, hand=True))
        d.text((110 + rnd.randint(-4, 4), y + 48), directions, fill=(10, 30, 120), font=_font(28, hand=True))
        y += 130
    d.text((w - 260, h - 120), "Sd/- Dr. Demo", fill=(10, 30, 120), font=_font(28, hand=True))
    img = img.rotate(rnd.uniform(-2, 2), expand=False, fillcolor=(240, 238, 230))
    path = OUT / f"{name}.png"
    img.save(path)
    return path


def main() -> None:
    OUT.mkdir(exist_ok=True)
    strip("strip_augmentin_625", "AUGMENTIN 625 DUO", [
        "Amoxycillin and Potassium Clavulanate Tablets IP",
        "Each film coated tablet contains:",
        "Amoxycillin Trihydrate IP eq. to Amoxycillin 500 mg",
        "Potassium Clavulanate Diluted IP eq. to Clavulanic Acid 125 mg",
    ], "223.40 for 10 tablets", "AUG4521", 1)
    strip("strip_glycomet_sr_500", "GLYCOMET-SR 500", [
        "Metformin Hydrochloride Sustained Release Tablets IP",
        "Each uncoated sustained release tablet contains:",
        "Metformin Hydrochloride IP 500 mg",
    ], "40.00 for 20 tablets", "GSR7781", 2)
    strip("strip_dolo_650_nsq", "DOLO 650", [
        "Paracetamol Tablets IP 650 mg",
        "Each uncoated tablet contains:",
        "Paracetamol IP 650 mg",
    ], "33.60 for 15 tablets", "DEMO123", 3)
    strip("strip_demo_fdc", "SAATHI-DEMO FDC", [
        "Aceclofenac, Paracetamol & Rabeprazole Tablets",
        "Each film coated tablet contains:",
        "Aceclofenac IP 100 mg, Paracetamol IP 325 mg, Rabeprazole Sodium IP 20 mg",
    ], "95.00 for 10 tablets", "FDC0001", 4)
    strip("bottle_calpol_250", "CALPOL 250 SUSPENSION", [
        "Paracetamol Paediatric Oral Suspension IP",
        "Each 5 ml contains: Paracetamol IP 250 mg",
        "60 ml bottle. Shake well before use.",
    ], "52.00 for 60 ml", "CP25009", 5)
    prescription("rx_glycomet_500", [("Tab. Glycomet 500", "1 - 0 - 1  after food x 30 days")], 6)
    prescription("rx_augmentin_625", [("Tab. Augmentin 625 Duo", "1 - 0 - 1  x 5 days after food"),
                                      ("Tab. Pan 40", "1 - 0 - 0  before breakfast x 5 days")], 7)
    prescription("rx_paracetamol_syrup_120", [("Syp. Paracetamol 120 mg / 5 ml", "5 ml SOS for fever")], 8)
    print("\n".join(sorted(str(p.relative_to(ROOT)) for p in OUT.glob("*.png"))))


if __name__ == "__main__":
    main()

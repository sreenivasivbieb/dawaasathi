"""Pack detector: zero-shot YOLO-World v2 (small) exported to ONNX with a fixed vocabulary.

Finds the medicine pack in a photo (strip / bottle / box) so OCR reads the pack, not the table or
the hand around it. Runs on CPU with onnxruntime only (~100-200 ms); no torch at runtime. The model
file is built once by scripts/export_yolo.py into var/models/pack_detector.onnx; without it this
stage is skipped and the whole photo is read.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

# Same order as scripts/export_yolo.py.
CLASSES = ["blister pack", "pill packet", "bottle", "box", "package", "label"]
PACK_CLASSES = {"blister pack", "pill packet", "bottle", "box", "package"}
SIZE = 640


@dataclass
class Detection:
    label: str
    conf: float
    box: tuple[int, int, int, int]  # x1, y1, x2, y2 in original pixels
    area: float  # share of the photo


@dataclass
class DetectResult:
    detections: list[Detection]
    crop: bytes | None  # JPEG of the padded pack region, when cropping helps
    used: Detection | None
    ms: float


class PackDetector:
    def __init__(self, model_path: Path, min_conf: float = 0.25, crop_below_area: float = 0.6):
        self.model_path = model_path
        self.min_conf = min_conf
        self.crop_below_area = crop_below_area
        self._session = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.model_path.exists()

    def _sess(self):
        if self._session is None:
            with self._lock:
                if self._session is None:
                    import onnxruntime as ort

                    opts = ort.SessionOptions()
                    opts.log_severity_level = 3
                    self._session = ort.InferenceSession(str(self.model_path), opts,
                                                         providers=["CPUExecutionProvider"])
        return self._session

    def detect(self, image: bytes) -> DetectResult:
        import time

        t0 = time.perf_counter()
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(image))).convert("RGB")
        w, h = img.size
        scale = SIZE / max(w, h)
        nw, nh = round(w * scale), round(h * scale)
        canvas = Image.new("RGB", (SIZE, SIZE), (114, 114, 114))
        px, py = (SIZE - nw) // 2, (SIZE - nh) // 2
        canvas.paste(img.resize((nw, nh), Image.BILINEAR), (px, py))
        x = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
        out = self._sess().run(None, {"images": x})[0][0]  # (4 + classes, 8400)
        dets = _decode(out, scale, px, py, w, h)
        used = next((d for d in dets if d.label in PACK_CLASSES and d.conf >= self.min_conf), None)
        crop = None
        if used is not None and used.area < self.crop_below_area:
            x1, y1, x2, y2 = used.box
            pad_x, pad_y = int((x2 - x1) * 0.08), int((y2 - y1) * 0.08)
            region = img.crop((max(0, x1 - pad_x), max(0, y1 - pad_y), min(w, x2 + pad_x), min(h, y2 + pad_y)))
            buf = io.BytesIO()
            region.save(buf, format="JPEG", quality=92)
            crop = buf.getvalue()
        return DetectResult(dets[:5], crop, used, round((time.perf_counter() - t0) * 1000, 1))


def _decode(out: np.ndarray, scale: float, px: int, py: int, w: int, h: int,
            conf_floor: float = 0.05, iou: float = 0.5) -> list[Detection]:
    scores = out[4:]
    cls = scores.argmax(0)
    conf = scores.max(0)
    keep = conf >= conf_floor
    if not keep.any():
        return []
    cx, cy, bw, bh = out[0][keep], out[1][keep], out[2][keep], out[3][keep]
    boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], 1)
    boxes[:, [0, 2]] = ((boxes[:, [0, 2]] - px) / scale).clip(0, w)
    boxes[:, [1, 3]] = ((boxes[:, [1, 3]] - py) / scale).clip(0, h)
    conf, cls = conf[keep], cls[keep]
    order = conf.argsort()[::-1]
    picked: list[int] = []
    for i in order:  # class-agnostic NMS: one label per region
        if all(_iou(boxes[i], boxes[j]) < iou for j in picked):
            picked.append(int(i))
        if len(picked) >= 10:
            break
    dets = []
    for i in picked:
        x1, y1, x2, y2 = (int(v) for v in boxes[i])
        dets.append(Detection(CLASSES[int(cls[i])], round(float(conf[i]), 3), (x1, y1, x2, y2),
                              round((x2 - x1) * (y2 - y1) / (w * h), 3)))
    return dets


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return float(inter / union) if union > 0 else 0.0

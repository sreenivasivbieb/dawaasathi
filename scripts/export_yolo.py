"""Export the zero-shot pack detector (YOLO-World v2 small) to ONNX with a fixed vocabulary.

Runtime needs only onnxruntime (app/adapters/detect.py). This script needs torch + ultralytics,
so run it once in a separate environment, e.g.:

    uv venv /tmp/yoloenv && VIRTUAL_ENV=/tmp/yoloenv uv pip install torch torchvision \
        --index-url https://download.pytorch.org/whl/cpu && VIRTUAL_ENV=/tmp/yoloenv uv pip install ultralytics onnx
    /tmp/yoloenv/bin/python scripts/export_yolo.py var/models/pack_detector.onnx
"""

import shutil
import sys
from pathlib import Path

from ultralytics import YOLOWorld

# Keep in sync with app/adapters/detect.py CLASSES (order matters).
CLASSES = ["blister pack", "pill packet", "bottle", "box", "package", "label"]


def main(out: str) -> None:
    model = YOLOWorld("yolov8s-worldv2.pt")
    model.set_classes(CLASSES)
    path = model.export(format="onnx", imgsz=640, simplify=True, opset=17)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(path, out)
    print("wrote", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "var/models/pack_detector.onnx")

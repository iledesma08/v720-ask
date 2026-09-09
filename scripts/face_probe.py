#!/usr/bin/env python3
"""Phase-1 probe for #20: YuNet face detection over saved snapshots.

Downloads the YuNet ONNX model once (~300KB), then reports faces and
milliseconds per image. No integration, no commits of models.

Usage (Pi):
    /tmp/v720new/bin/python scripts/face_probe.py --dir /mnt/data/docker/opencode/projects/v720-ask/snapshots
    /tmp/v720new/bin/python scripts/face_probe.py --dir /tmp --model /tmp/yunet.onnx
"""

import argparse
import glob
import os
import sys
import time
import urllib.request

MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")


def ensure_model(path: str) -> str:
    if os.path.exists(path):
        return path
    print(f"downloading YuNet model to {path} ...")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    urllib.request.urlretrieve(MODEL_URL, path)
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="snapshots")
    ap.add_argument("--model", default=os.path.expanduser("~/.cache/yunet.onnx"))
    args = ap.parse_args()

    import cv2 as _cv2

    model = ensure_model(args.model)
    det = _cv2.FaceDetectorYN_create(
        model, "", (320, 320), 0.6, 0.3, 5000)
    imgs = sorted(glob.glob(os.path.join(args.dir, "*.jpg")))
    if not imgs:
        print("no jpgs in", args.dir)
        return 1
    for path in imgs:
        img = _cv2.imread(path)
        if img is None:
            print(f"{os.path.basename(path)}: unreadable")
            continue
        h, w = img.shape[:2]
        det.setInputSize((w, h))
        t0 = time.monotonic()
        ok, faces = det.detect(img)
        ms = (time.monotonic() - t0) * 1000.0
        n = 0 if faces is None else len(faces)
        print(f"{os.path.basename(path)}: {w}x{h} faces={n} {ms:.0f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

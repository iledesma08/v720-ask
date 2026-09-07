#!/usr/bin/env python3
"""Headless check against V720/Naxclow camera in AP-directo mode.

Usage (from repo root, Pi with wlan0 -> Nax_*):
    python3 -m venv /tmp/v720fp && /tmp/v720fp/bin/pip install -r requirements-min.txt
    PYTHONPATH=src /tmp/v720fp/bin/python scripts/v720_check.py \
        --host 192.168.169.1 --port 6123 --frames 3 --out /tmp

Validates: connect (115 -> 501 -> 502/4 baseinfo), prints version,
captures N JPEGs (reassembled FFD8..FFD9) to --out/frameN.jpg.
Retries init up to 3x (5s apart): after an abrupt live cut the camera
answers control with CMD 6 PCM once, then accepts a fresh connect.
No cv2, no imshow, WARN logs only. Exit 0 on success.
"""

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from log import log  # noqa: E402


def _init_cam(netcl_tcp, v720_ap, host, port, attempts=3):
    """init_live_motion with retries: after an abrupt live cut the camera
    answers control with CMD 6 PCM and closes; a fresh connect retries clean."""
    last = None
    for i in range(1, attempts + 1):
        try:
            sock = netcl_tcp(host, port)
            sock.open()
            cam = v720_ap(sock)
            cam.init_live_motion()  # prints FW version
            return cam, sock
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"INIT_TRY {i}/{attempts} FAIL: {type(exc).__name__}")
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)
    raise last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.169.1")
    ap.add_argument("--port", type=int, default=6123)
    ap.add_argument("--frames", type=int, default=3)
    ap.add_argument("--out", default="/tmp")
    args = ap.parse_args()

    log.set_log_lvl(logging.WARN)

    from netcl_tcp import netcl_tcp
    from prot_ap import prot_ap
    from v720_ap import v720_ap

    saved, buf, sync = 0, bytearray(), False

    def on_data(cmd: int, data: bytes) -> None:
        nonlocal saved, buf, sync
        if cmd == 1:  # JPEG fragment
            if not sync:
                f = data.find(b"\xff\xd8")
                if f != -1:
                    buf.extend(data[f:])
                    sync = True
            else:
                f = data.find(b"\xff\xd9")
                if f != -1:
                    buf.extend(data[: f + 2])
                    path = os.path.join(args.out, f"frame{saved}.jpg")
                    with open(path, "wb") as fh:
                        fh.write(buf)
                    print(f"FRAME {saved} {len(buf)}B {path}")
                    saved += 1
                    buf.clear()
                    sync = False
                    if saved >= args.frames:
                        raise StopIteration
                else:
                    buf.extend(data)

    cam, sock = _init_cam(netcl_tcp, v720_ap, args.host, args.port)
    try:
        info = cam.baseinfo()
        if info is None or not getattr(info, "content", None):
            print("BASEINFO_FAIL")
            return 2
        print("BASEINFO:", info.content)
        try:
            cam.cap_live(on_data)
        except StopIteration:
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"CAP_FAIL: {type(exc).__name__} {exc}")
            return 1
    finally:
        try:
            # blind close: leave no stream running or the next fresh
            # connect only gets unsolicited CMD 6 PCM (see #10)
            sock.send(
                prot_ap(
                    content={"code": 0, "devTarget": "deadbeef"}
                ).req()
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass

    print(f"OK {saved} frames in {args.out}")
    return 0 if saved >= args.frames else 1


if __name__ == "__main__":
    raise SystemExit(main())

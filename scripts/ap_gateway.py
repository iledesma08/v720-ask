#!/usr/bin/env python3
"""AP-direct MJPEG gateway (single camera, single viewer).

Bridges the camera at 192.168.169.1:6123 (AP mode, v720_ap protocol)
to HTTP on the Pi's LAN side. Stdlib only + src/ imports, no cv2.

Usage (Pi, wlan0 -> Nax_*):
    PYTHONPATH=src /tmp/v720fp/bin/python scripts/ap_gateway.py \
        --camera 192.168.169.1:6123 --listen 0.0.0.0 --port 8090

Routes:
    GET /dev/list                  JSON [{"uid":"ap-camera",...}]
    GET /dev/ap-camera/live        multipart/x-mixed-replace MJPEG
    GET /dev/ap-camera/snapshot    single image/jpeg

Each viewer opens its own camera connection and closes it (code 0)
on disconnect, so the camera never stays poisoned (see #10).
Single viewer at a time: a second /live waits for the first to leave.
"""

import argparse
import json
import logging
import os
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from log import log  # noqa: E402

CAMERA = ("192.168.169.1", 6123)
UID = "ap-camera"
BOUNDARY = "jpgboundary"
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
SNAP_DIR = os.environ.get(
    "SNAP_DIR", os.path.join(os.path.dirname(__file__), "..", "snapshots"))
_cam_lock = threading.Lock()

# Serializes all writes on a shared live socket (605 confirms vs PTZ).
_send_lock = threading.Lock()
# Socket of the active live viewer, if any: PTZ rides on it so moving
# works while watching (STA precedent: control + media over one TCP).
_live_sock = None
_live_sock_lock = threading.Lock()


def _send_pkt(sock, data) -> None:
    with _send_lock:
        sock.send(data)


def _live_register(sock) -> None:
    global _live_sock
    with _live_sock_lock:
        _live_sock = sock

# Latest completed JPEG per gateway, published by the live reader thread.
# Lets /snapshot serve from cache while a viewer holds the camera lock.
_latest_lock = threading.Lock()
_latest_img: bytes | None = None
_latest_ts = 0.0
LATEST_MAX_AGE = 3.0


def _shot_name_ok(name: str) -> bool:
    """Allow only our own snapshot/clip file names (no traversal)."""
    import re

    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.(jpg|mp4)",
                        name or "") is not None


def _list_shots(day: str | None = None) -> list:
    """Saved shots+clips newest-first: [{name, day, time, kind}]."""
    import re

    out = []
    try:
        names = os.listdir(SNAP_DIR)
    except OSError:
        return out
    for name in sorted(names, reverse=True):
        m = re.fullmatch(r"[A-Za-z0-9-]+-(\d{8})-(\d{6})(-\d+)?\.(jpg|mp4)",
                          name)
        if not m:
            continue
        if day and m.group(1) != day:
            continue
        t = m.group(2)
        out.append({"name": name, "day": m.group(1),
                    "time": f"{t[0:2]}:{t[2:4]}:{t[4:6]}",
                    "kind": "video" if m.group(4) == "mp4" else "shot"})
    return out


def _grab_frame():
    """Single capture attempt, None when busy/failing. For periodic worker."""
    if not _cam_lock.acquire(blocking=False):
        return None
    try:
        sock = _open_cam(*CAMERA)
        try:
            _open_video(sock)
            return next(_iter_jpegs(sock, lambda: False), None)
        finally:
            _close_cam(sock)
    except Exception:  # noqa: BLE001
        return None
    finally:
        _cam_lock.release()


def _periodic_worker(interval: float):
    """Save a frame every interval seconds; skip when busy. Daemon."""
    import time as _time

    while True:
        _time.sleep(interval)
        try:
            img = _grab_frame()
        except Exception:  # noqa: BLE001
            continue
        if img is not None:
            try:
                _save_snapshot(img)
            except OSError:
                pass


def _record_clip(seconds: float):
    """Capture `seconds` of live JPEGs and mux to timestamped .mp4.

    Returns the file name, or None when no frames arrived. Needs
    numpy+opencv (requirements-min); raises RuntimeError without them.
    Caller must hold _cam_lock.
    """
    import datetime as _dt
    import time as _time

    try:
        import numpy as _np
        import cv2 as _cv2
    except ImportError as exc:
        raise RuntimeError("clip needs numpy+opencv (see requirements-min)") \
            from exc

    sock = _open_cam(*CAMERA)
    frames = []
    t0 = _time.monotonic()
    try:
        _open_video(sock)
        deadline = t0 + seconds + 10.0
        end = t0 + seconds
        for img in _iter_jpegs(sock, lambda: _time.monotonic() >= end):
            frames.append(img)
            if _time.monotonic() >= deadline:
                break
    finally:
        _close_cam(sock)
    if not frames:
        return None
    dec = [_cv2.imdecode(_np.frombuffer(f, dtype=_np.uint8),
                         _cv2.IMREAD_COLOR) for f in frames]
    dec = [d for d in dec if d is not None]
    if not dec:
        return None
    h, w = dec[0].shape[:2]
    os.makedirs(SNAP_DIR, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{UID}-{stamp}.mp4"
    path = os.path.join(SNAP_DIR, name)
    n = 1
    while os.path.exists(path):
        n += 1
        name = f"{UID}-{stamp}-{n}.mp4"
        path = os.path.join(SNAP_DIR, name)
    tmp = os.path.join(SNAP_DIR, ".part-" + name)  # keep .mp4 suffix
    elapsed = _time.monotonic() - t0
    fps = min(max(len(dec) / max(elapsed, 0.1), 1.0), 15.0)
    if not _mux_ffmpeg(dec, tmp, w, h, fps):
        _mux_cv2(dec, tmp, w, h)
    os.rename(tmp, path)
    return name


def _mux_ffmpeg(dec, tmp, w, h, fps) -> bool:
    """H.264 mp4 via ffmpeg pipe (plays inline in browsers)."""
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        return False
    try:
        proc = subprocess.Popen(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "rawvideo", "-pix_fmt", "bgr24",
             "-s", f"{w}x{h}", "-r", f"{fps:.2f}", "-i", "-",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", tmp],
            stdin=subprocess.PIPE)
    except OSError:
        return False
    try:
        for d in dec:
            if d.shape[:2] == (h, w):
                proc.stdin.write(d.tobytes())
        proc.stdin.close()
        return proc.wait(timeout=30) == 0 and os.path.exists(tmp)
    except (OSError, ValueError):
        try:
            proc.kill()
        except OSError:
            pass
        return False


def _mux_cv2(dec, tmp, w, h) -> None:
    """Fallback: mp4v via opencv (downloads fine, browsers may not play)."""
    import cv2 as _cv2

    vw = None
    for tag in ("mp4v", "XVID"):
        vw = _cv2.VideoWriter(tmp, _cv2.VideoWriter_fourcc(*tag), 10, (w, h))
        if vw.isOpened():
            break
        vw.release()
        vw = None
    if vw is None:
        raise RuntimeError(f"VideoWriter open failed ({w}x{h}, mp4v/XVID)")
    for d in dec:
        if d.shape[:2] == (h, w):
            vw.write(d)
    vw.release()


def _save_snapshot(img: bytes) -> str:
    """Store img timestamped under SNAP_DIR, return the file name."""
    import datetime as _dt

    os.makedirs(SNAP_DIR, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{UID}-{stamp}.jpg"
    path = os.path.join(SNAP_DIR, name)
    n = 1
    while os.path.exists(path):
        n += 1
        name = f"{UID}-{stamp}-{n}.jpg"
        path = os.path.join(SNAP_DIR, name)
    with open(path, "wb") as fh:
        fh.write(img)
    return name


def _ptz_packet(direction: int) -> bytes:
    from prot_ap import prot_ap
    import cmd_udp

    return prot_ap(content={
        "code": cmd_udp.CODE_FORWARD_DEV_MOTOR_STATE,
        "devTarget": "deadbeef",
        "motorState": direction,
    }).req()


def _ptz_repeat(sock, direction: int, ms: int) -> None:
    """Repeat 212 sends every 80 ms for `ms` (app drive behavior)."""
    import time as _time

    end = _time.monotonic() + ms / 1000.0
    pkt = _ptz_packet(direction)
    while True:
        _send_pkt(sock, pkt)
        remaining = end - _time.monotonic()
        if remaining <= 0:
            break
        _time.sleep(min(0.08, remaining))


def _resync_request(sock, data, match, tries=30):
    """Send once, then drain until a response matching match() arrives.

    A wedged camera lead-unsolicited CMD 6 PCM (or stale JSON) ahead of
    solicited responses (probe: 502/4 got 501's answer). match() receives
    either the decoded JSON dict (cmd 0) or the prot_udp packet.
    """
    from prot_udp import prot_udp
    import json as _json

    sock.send(data)
    for _ in range(tries):
        try:
            raw = sock.recv()
        except Exception:  # noqa: BLE001 - heartbeat skip / timeout: keep draining
            continue
        p = prot_udp.resp(raw)
        if p is None:
            continue
        if p.cmd == 0:
            try:
                j = _json.loads(bytes(p.payload).decode())
            except Exception:  # noqa: BLE001
                continue
            if match(j):
                return j
        elif match(p):
            return p
    raise TimeoutError("no matching response after drain")


def _open_cam(host, port, attempts=2):
    """Handshake with drain-resync: tolerates PCM backlog (see #11)."""
    import time as _time

    from netcl_tcp import netcl_tcp
    from prot_udp import prot_udp
    from prot_json_udp import prot_json_udp
    from prot_ap import prot_ap
    import cmd_udp

    last = None
    for _ in range(attempts):
        sock = netcl_tcp(host, port)
        try:
            sock.open()
            sock._socket.settimeout(4)
            now = int(_time.time())
            _resync_request(
                sock,
                prot_udp(cmd=cmd_udp.P2P_UDP_CMD_LIVE_MOTION).req(),
                lambda m: not isinstance(m, dict) and m.cmd == 115,
            )
            _resync_request(
                sock,
                prot_json_udp(json={
                    "code": cmd_udp.CODE_AP_CONNECT,
                    "target": "deadbeef",
                    "token": "This is TEST token",
                    "unixTimer": now,
                }).req(),
                lambda m: isinstance(m, dict) and m.get("code") == 501,
            )
            info = _resync_request(
                sock,
                prot_ap(content={
                    "code": cmd_udp.CODE_FORWARD_DEV_BASE_INFO,
                    "devTarget": "deadbeef",
                    "unixTimer": now,
                }).req(),
                lambda m: isinstance(m, dict) and m.get("code") == 502,
            )
            try:
                print("CAM FW:", info["content"]["version"])
            except (KeyError, TypeError):
                print("CAM FW: unknown")
            sock._socket.settimeout(30)
            return sock
        except Exception as exc:  # noqa: BLE001
            last = exc
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass
            _time.sleep(3)
    raise last


def _open_video(sock):
    """Ask the camera to start pushing audio+video (fire and forget;
    the ack arrives in-stream and is ignored by the reader)."""
    import time as _time

    from prot_ap import prot_ap
    import cmd_udp

    sock.send(prot_ap(content={
        "code": cmd_udp.CODE_FORWARD_OPEN_A_OPEN_V,
        "devTarget": "deadbeef",
        "unixTimer": int(_time.time()),
    }).req())


def _close_cam(sock):
    from prot_ap import prot_ap

    try:
        _send_pkt(sock, prot_ap(content={"code": 0,
                                         "devTarget": "deadbeef"}).req())
    except Exception:  # noqa: BLE001
        pass
    try:
        sock.close()
    except Exception:  # noqa: BLE001
        pass


def _iter_jpegs(sock, stop, confirm_interval=0.1):
    """Yield complete JPEG frames until stop() is true.

    Reads raw packets (keeps pkg_ids) and sends P2P_UDP_CMD_RETRANSMISSION_CONFIRM
    (605) every confirm_interval seconds: without it the camera stops pushing
    after ~4s (stall probe: 111 pkts then silence). Same contract as the STA
    data channel (fake_server.md section 9), but over the AP TCP connection.
    """
    import time as _time

    from prot_udp import prot_udp
    import cmd_udp

    buf, sync = bytearray(), False
    out: "queue.Queue[bytes]" = queue.Queue(maxsize=8)
    stop_flag = threading.Event()

    def reader():
        global _latest_img, _latest_ts
        nonlocal buf, sync
        pending = []
        last_confirm = _time.monotonic()
        sock._socket.settimeout(5)
        while not stop_flag.is_set():
            try:
                raw = sock.recv()
            except Exception:  # noqa: BLE001 - heartbeat skip: counts as alive
                continue
            now = _time.monotonic()
            if now - last_confirm >= confirm_interval:
                payload = bytearray()
                for pid in pending:
                    payload.extend(int.to_bytes(pid, 4, "little"))
                pending.clear()
                try:
                    _send_pkt(sock, prot_udp(
                        payload=payload,
                        cmd=cmd_udp.P2P_UDP_CMD_RETRANSMISSION_CONFIRM,
                    ).req())
                except Exception:  # noqa: BLE001
                    return
                last_confirm = now
            p = prot_udp.resp(raw)
            if p is None:
                continue
            if p.cmd in (cmd_udp.P2P_UDP_CMD_JPEG,
                         cmd_udp.P2P_UDP_CMD_G711,
                         cmd_udp.P2P_UDP_CMD_AVI):
                pending.append(p._pkg_id)
            if p.cmd != cmd_udp.P2P_UDP_CMD_JPEG:
                continue
            data = bytes(p.payload)
            if not sync:
                f = data.find(b"\xff\xd8")
                if f != -1:
                    buf.extend(data[f:])
                    sync = True
            else:
                f = data.find(b"\xff\xd9")
                if f != -1:
                    buf.extend(data[: f + 2])
                    frame = bytes(buf)
                    try:
                        out.put_nowait(frame)
                    except queue.Full:
                        pass
                    with _latest_lock:
                        _latest_img = frame
                        _latest_ts = _time.monotonic()
                    buf.clear()
                    sync = False
                else:
                    buf.extend(data)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    try:
        while not stop():
            try:
                yield out.get(timeout=6)
            except queue.Empty:
                return
    finally:
        stop_flag.set()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "APGateway/1.0"

    def log_message(self, *args):
        logging.getLogger("ap-gateway").info(*args)

    def _send(self, code, ctype, length=None, extra=()):
        self.send_response(code)
        self.send_header("Content-type", ctype)
        if length is not None:
            self.send_header("Content-length", str(length))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):  # noqa: N802
        from urllib.parse import urlparse, parse_qs

        parts = urlparse(self.path)
        route = parts.path
        if route in ("/", "/index.html"):
            self._static()
        elif route == "/dbg":
            body = (b"<!doctype html><html><body><p id=\"m\">JS did NOT run</p>"
                    b"<script>document.getElementById('m').textContent='JS-WORKS ';"
                    b"fetch('dev/list').then(function(r){return r.text();}).then("
                    b"function(t){document.getElementById('m').textContent+=' FETCH-OK:'+t;},"
                    b"function(e){document.getElementById('m').textContent+=' FETCH-FAIL:'+e;});"
                    b"</script></body></html>")
            self._send(200, "text/html; charset=utf-8", len(body),
                       [("Connection", "close"), ("Cache-Control", "no-store")])
            self.wfile.write(body)
        elif route == "/dev/list":
            body = json.dumps(
                [{"uid": UID, "host": CAMERA[0], "port": CAMERA[1]}]
            ).encode()
            self._send(200, "application/json", len(body),
                       [("Connection", "close")])
            self.wfile.write(body)
        elif route == f"/dev/{UID}/snapshot":
            self._snapshot()
        elif route == f"/dev/{UID}/live":
            self._live()
        elif route == "/dev/shots":
            query = parse_qs(parts.query)
            body = json.dumps(
                _list_shots(query.get("day", [None])[0])).encode()
            self._send(200, "application/json", len(body),
                       [("Connection", "close")])
            self.wfile.write(body)
        elif route.startswith("/dev/shots/"):
            name = route[len("/dev/shots/"):]
            if not _shot_name_ok(name):
                self._send(400, "text/plain", 11,
                           [("Connection", "close")])
                self.wfile.write(b"bad filename")
                return
            try:
                with open(os.path.join(SNAP_DIR, name), "rb") as fh:
                    img = fh.read()
            except OSError:
                self._send(404, "text/plain", 9,
                           [("Connection", "close")])
                self.wfile.write(b"not found")
                return
            ctype = ("video/mp4" if name.endswith(".mp4")
                     else "image/jpeg")
            self._send(200, ctype, len(img),
                       [("Connection", "close")])
            self.wfile.write(img)
        else:
            self._send(404, "text/plain", 9,
                       [("Connection", "close")])
            self.wfile.write(b"not found")

    def do_DELETE(self):  # noqa: N802
        from urllib.parse import urlparse

        route = urlparse(self.path).path
        if not route.startswith("/dev/shots/"):
            self._send(404, "text/plain", 9,
                       [("Connection", "close")])
            self.wfile.write(b"not found")
            return
        name = route[len("/dev/shots/"):]
        if not _shot_name_ok(name):
            self._send(400, "text/plain", 11,
                       [("Connection", "close")])
            self.wfile.write(b"bad filename")
            return
        try:
            os.unlink(os.path.join(SNAP_DIR, name))
        except OSError:
            self._send(404, "text/plain", 9,
                       [("Connection", "close")])
            self.wfile.write(b"not found")
            return
        body = json.dumps({"deleted": name}).encode()
        self._send(200, "application/json", len(body),
                   [("Connection", "close")])
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        from urllib.parse import urlparse, parse_qs

        parts = urlparse(self.path)
        route = parts.path
        query = parse_qs(parts.query)
        if route == f"/dev/{UID}/ptz":
            self._ptz(query)
            return
        if route != f"/dev/{UID}/clip":
            self._send(404, "text/plain", 9,
                       [("Connection", "close")])
            self.wfile.write(b"not found")
            return
        try:
            seconds = float(parse_qs(parts.query).get("seconds", ["10"])[0])
        except ValueError:
            seconds = 10.0
        seconds = min(max(seconds, 3.0), 60.0)
        import time as _time

        acquired = False
        for _ in range(20):
            # The page pauses its own live feed first; give the previous
            # session a moment to notice the closed socket and release.
            if _cam_lock.acquire(blocking=False):
                acquired = True
                break
            _time.sleep(0.5)
        if not acquired:
            self._send(503, "text/plain", 11,
                       [("Connection", "close")])
            self.wfile.write(b"camera busy")
            return
        try:
            name = _record_clip(seconds)
        except RuntimeError as exc:
            self._send(501, "text/plain", len(str(exc)),
                       [("Connection", "close")])
            self.wfile.write(str(exc).encode())
            return
        except Exception as exc:  # noqa: BLE001
            self._send(502, "text/plain", len(str(exc)),
                       [("Connection", "close")])
            self.wfile.write(str(exc).encode())
            return
        finally:
            _cam_lock.release()
        if name is None:
            self._send(502, "text/plain", 17,
                       [("Connection", "close")])
            self.wfile.write(b"no frames in time")
            return
        body = json.dumps({"clip": name, "seconds": seconds}).encode()
        self._send(200, "application/json", len(body),
                   [("Connection", "close")])
        self.wfile.write(body)

    def _static(self):
        """Serve the bundled index page (exact file only, no listing)."""
        try:
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as fh:
                body = fh.read()
        except OSError:
            self._send(404, "text/plain", 9,
                       [("Connection", "close")])
            self.wfile.write(b"not found")
            return
        self._send(200, "text/html; charset=utf-8", len(body),
                   [("Connection", "close"),
                    ("Cache-Control", "no-store, must-revalidate"),
                    ("Pragma", "no-cache"),
                    ("Expires", "0")])
        self.wfile.write(body)

    def _sleep_or_gone(self, delay):
        """Sleep delay seconds, aborting early if the viewer went away."""
        import time as _time

        end = _time.monotonic() + delay
        while True:
            if self.wfile.closed:
                return True
            now = _time.monotonic()
            if now >= end:
                return False
            _time.sleep(min(0.2, end - now))

    def _ptz(self, query) -> None:
        """Move the mount: ?dir=0..4 (0 stop/calibrate) &ms=80..2000.

        Rides the active live socket when a viewer holds it (control and
        media share one TCP, STA precedent); else a dedicated connection.
        Sends are fire-and-forget repeats every 80 ms, like the vendor app.
        """
        global _live_sock
        try:
            direction = int(query.get("dir", ["-1"])[0])
        except ValueError:
            direction = -1
        if direction not in (0, 1, 2, 3, 4):
            self._send(400, "text/plain", 14,
                       [("Connection", "close")])
            self.wfile.write(b"bad direction")
            return
        try:
            ms = int(query.get("ms", ["400"])[0])
        except ValueError:
            ms = 400
        ms = min(max(ms, 80), 2000)

        with _live_sock_lock:
            shared = _live_sock
        if shared is not None:
            try:
                _ptz_repeat(shared, direction, ms)
            except Exception as exc:  # noqa: BLE001 - stale socket: fall through
                log("ap-gateway").warn("ptz: shared socket dead (%s)", exc)
                with _live_sock_lock:
                    if _live_sock is shared:
                        _live_sock = None
                shared = None
            else:
                body = json.dumps({"moved": direction, "ms": ms,
                                   "via": "live"}).encode()
                self._send(200, "application/json", len(body),
                           [("Connection", "close")])
                self.wfile.write(body)
                return
        if not _cam_lock.acquire(blocking=False):
            self._send(503, "text/plain", 11,
                       [("Connection", "close")])
            self.wfile.write(b"camera busy")
            return
        try:
            sock = _open_cam(*CAMERA)
            try:
                _ptz_repeat(sock, direction, ms)
            finally:
                _close_cam(sock)
        except Exception as exc:  # noqa: BLE001
            self._send(502, "text/plain", len(str(exc)),
                       [("Connection", "close")])
            self.wfile.write(str(exc).encode())
            return
        finally:
            _cam_lock.release()
        body = json.dumps({"moved": direction, "ms": ms,
                           "via": "direct"}).encode()
        self._send(200, "application/json", len(body),
                   [("Connection", "close")])
        self.wfile.write(body)

    def _snapshot(self):
        import time as _time
        from urllib.parse import urlparse, parse_qs

        save = parse_qs(urlparse(self.path).query).get("save", ["0"])[0] == "1"
        with _latest_lock:
            img = (_latest_img
                   if _latest_img is not None
                   and _time.monotonic() - _latest_ts < LATEST_MAX_AGE
                   else None)
        if img is not None:
            return self._serve_snapshot(img, save)
        if not _cam_lock.acquire(blocking=False):
            self._send(503, "text/plain", 11,
                       [("Connection", "close")])
            self.wfile.write(b"camera busy")
            return
        gwlog = log("ap-gateway")
        max_retries = 3
        delay = 2.0
        try:
            img = None
            for attempt in range(max_retries + 1):
                try:
                    sock = _open_cam(*CAMERA)
                    try:
                        _open_video(sock)
                        frames = _iter_jpegs(sock, lambda: False)
                        img = next(frames, None)
                    finally:
                        _close_cam(sock)
                except Exception as exc:  # noqa: BLE001 - reopen on drop
                    gwlog.warn("snapshot: attempt %d/%d failed (%s)",
                               attempt + 1, max_retries + 1, exc)
                    img = None
                if img is not None:
                    break
                if attempt < max_retries:
                    gwlog.warn("snapshot: no frame, retry %d/%d in %.0fs",
                               attempt + 1, max_retries, delay)
                    _time.sleep(delay)
                    delay = min(delay * 2, 60.0)
            if img is None:
                self._send(502, "text/plain", 17,
                           [("Connection", "close")])
                self.wfile.write(b"no frame in time")
                return
            return self._serve_snapshot(img, save)
        except Exception as exc:  # noqa: BLE001
            self._send(502, "text/plain", len(str(exc)),
                       [("Connection", "close")])
            self.wfile.write(str(exc).encode())
        finally:
            _cam_lock.release()

    def _serve_snapshot(self, img: bytes, save: bool):
        if save:
            name = _save_snapshot(img)
            body = json.dumps({"saved": name}).encode()
            self._send(200, "application/json", len(body),
                       [("Connection", "close")])
            self.wfile.write(body)
            return
        self._send(200, "image/jpeg", len(img),
                   [("Connection", "close")])
        self.wfile.write(img)

    def _live(self):
        if not _cam_lock.acquire(blocking=False):
            self._send(503, "text/plain", 11,
                       [("Connection", "close")])
            self.wfile.write(b"camera busy")
            return
        gwlog = log("ap-gateway")
        max_retries = 5
        delay = 2.0
        max_delay = 60.0
        retries = 0
        headers_sent = False
        sock = None
        try:
            while True:
                try:
                    sock = _open_cam(*CAMERA)
                    _open_video(sock)
                    _live_register(sock)
                except Exception as exc:  # noqa: BLE001 - open failed: retry
                    gwlog.warn("live: open failed (%s), retry %d/%d",
                               exc, retries + 1, max_retries)
                    if sock is not None:
                        _close_cam(sock)
                        _live_register(None)
                        sock = None
                    retries += 1
                    if retries > max_retries:
                        if not headers_sent:
                            try:
                                msg = b"camera unavailable"
                                self._send(502, "text/plain", len(msg),
                                           [("Connection", "close")])
                                self.wfile.write(msg)
                            except (BrokenPipeError, ConnectionResetError):
                                pass
                        else:
                            gwlog.warn("live: giving up after %d retries",
                                       max_retries)
                        break
                    if self._sleep_or_gone(delay):
                        break
                    delay = min(delay * 2, max_delay)
                    continue
                if not headers_sent:
                    try:
                        self.send_response(200)
                        self.send_header(
                            "Content-type",
                            f"multipart/x-mixed-replace; boundary={BOUNDARY}")
                        self.send_header("Connection", "close")
                        self.send_header("Pragma", "no-cache")
                        self.end_headers()
                    except (BrokenPipeError, ConnectionResetError):
                        _close_cam(sock)
                        _live_register(None)
                        sock = None
                        break
                    headers_sent = True
                client_gone = False
                try:
                    for img in _iter_jpegs(sock, lambda: self.wfile.closed):
                        retries = 0
                        delay = 2.0
                        try:
                            self.wfile.write(f"--{BOUNDARY}\r\n".encode())
                            self.wfile.write(b"Content-type: image/jpeg\r\n")
                            self.wfile.write(
                                f"Content-length: {len(img)}\r\n\r\n".encode())
                            self.wfile.write(img)
                            self.wfile.write(b"\r\n")
                        except (BrokenPipeError, ConnectionResetError):
                            client_gone = True
                            break
                except Exception as exc:  # noqa: BLE001 - stream died: retry
                    gwlog.warn("live: stream error (%s)", exc)
                finally:
                    if sock is not None:
                        _close_cam(sock)
                        _live_register(None)
                        sock = None
                if client_gone or self.wfile.closed:
                    break
                retries += 1
                if retries > max_retries:
                    gwlog.warn("live: giving up after %d retries", max_retries)
                    break
                gwlog.warn("live: stream died, retry %d/%d in %.0fs",
                           retries, max_retries, delay)
                if self._sleep_or_gone(delay):
                    break
                delay = min(delay * 2, max_delay)
        finally:
            if sock is not None:
                try:
                    _close_cam(sock)
                except Exception:  # noqa: BLE001
                    pass
            _live_register(None)
            _cam_lock.release()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera", default="192.168.169.1:6123")
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--snap-every", type=float,
                    default=float(os.environ.get("SNAP_EVERY_SEC", "0")),
                    help="periodic snapshot interval in seconds, 0 disables")
    args = ap.parse_args()
    log.set_log_lvl(logging.WARN)

    global CAMERA
    host, _, port = args.camera.partition(":")
    CAMERA = (host, int(port or 6123))

    if args.snap_every > 0:
        th = threading.Thread(target=_periodic_worker,
                              args=(args.snap_every,), daemon=True)
        th.start()
        print(f"periodic snapshots every {args.snap_every}s into {SNAP_DIR}")

    srv = ThreadingHTTPServer((args.listen, args.port), Handler)
    print(f"serving {UID} {CAMERA[0]}:{CAMERA[1]} on "
          f"{args.listen}:{args.port} (single viewer)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

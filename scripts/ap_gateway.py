#!/usr/bin/env python3
"""AP-directo MJPEG gateway (single camera, single viewer).

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
_cam_lock = threading.Lock()


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
        sock.send(prot_ap(content={"code": 0, "devTarget": "deadbeef"}).req())
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
                    sock.send(prot_udp(
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
                    try:
                        out.put_nowait(bytes(buf))
                    except queue.Full:
                        pass
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
        if self.path == "/dev/list":
            body = json.dumps(
                [{"uid": UID, "host": CAMERA[0], "port": CAMERA[1]}]
            ).encode()
            self._send(200, "application/json", len(body),
                       [("Connection", "close")])
            self.wfile.write(body)
        elif self.path == f"/dev/{UID}/snapshot":
            self._snapshot()
        elif self.path == f"/dev/{UID}/live":
            self._live()
        else:
            self._send(404, "text/plain", 9,
                       [("Connection", "close")])
            self.wfile.write(b"not found")

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

    def _snapshot(self):
        import time as _time

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
            self._send(200, "image/jpeg", len(img),
                       [("Connection", "close")])
            self.wfile.write(img)
        except Exception as exc:  # noqa: BLE001
            self._send(502, "text/plain", len(str(exc)),
                       [("Connection", "close")])
            self.wfile.write(str(exc).encode())
        finally:
            _cam_lock.release()

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
                except Exception as exc:  # noqa: BLE001 - open failed: retry
                    gwlog.warn("live: open failed (%s), retry %d/%d",
                               exc, retries + 1, max_retries)
                    if sock is not None:
                        _close_cam(sock)
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
            _cam_lock.release()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera", default="192.168.169.1:6123")
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()
    log.set_log_lvl(logging.WARN)

    global CAMERA
    host, _, port = args.camera.partition(":")
    CAMERA = (host, int(port or 6123))

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

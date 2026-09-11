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
    GET /dev/settings              runtime settings JSON
    POST /dev/settings             validate + persist settings JSON
    POST /dev/ap-camera/ir?on=0|1  IR LED (manual, #28 phase 1)

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
SETTINGS_PATH = os.path.join(SNAP_DIR, "settings.json")
_SETTINGS_LOCK = threading.Lock()
DEFAULT_SETTINGS = {
    "facewatch_enabled": True,
    "facewatch_interval_sec": 20.0,
    "motion_thresh": 10.0,
    "night_ir_mode": "off",
    "capture_mode": "shots",
    "capture_trigger": "both",
    "clip_sec": 10.0,
    "clip_cooldown_sec": 30.0,
    "telegram_enabled": False,
    "alert_start_hour": 0,
    "alert_end_hour": 5,
    "alert_cooldown_sec": 300.0,
}


def _load_settings():
    """Merged runtime settings; defaults when missing/corrupt."""
    try:
        with open(SETTINGS_PATH) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)
    if not isinstance(data, dict):
        return dict(DEFAULT_SETTINGS)
    out = dict(DEFAULT_SETTINGS)
    en = data.get("facewatch_enabled", out["facewatch_enabled"])
    out["facewatch_enabled"] = bool(en) if isinstance(en, (bool, int)) else True
    try:
        iv = float(data.get("facewatch_interval_sec",
                            out["facewatch_interval_sec"]))
    except (TypeError, ValueError):
        iv = out["facewatch_interval_sec"]
    out["facewatch_interval_sec"] = min(max(iv, 2.0), 300.0)
    try:
        th = float(data.get("motion_thresh", out["motion_thresh"]))
    except (TypeError, ValueError):
        th = out["motion_thresh"]
    out["motion_thresh"] = min(max(th, 1.0), 100.0)
    mode = data.get("night_ir_mode", out["night_ir_mode"])
    out["night_ir_mode"] = mode if mode in ("off", "on") else "off"
    cmode = data.get("capture_mode", out["capture_mode"])
    out["capture_mode"] = cmode if cmode in ("shots", "clips", "off") \
        else "shots"
    trig = data.get("capture_trigger", out["capture_trigger"])
    out["capture_trigger"] = trig if trig in ("motion", "faces", "both") \
        else "both"
    try:
        cs = float(data.get("clip_sec", out["clip_sec"]))
    except (TypeError, ValueError):
        cs = out["clip_sec"]
    out["clip_sec"] = min(max(cs, 3.0), 60.0)
    try:
        cd = float(data.get("clip_cooldown_sec", out["clip_cooldown_sec"]))
    except (TypeError, ValueError):
        cd = out["clip_cooldown_sec"]
    out["clip_cooldown_sec"] = min(max(cd, 5.0), 300.0)
    tg = data.get("telegram_enabled", out["telegram_enabled"])
    out["telegram_enabled"] = bool(tg) if isinstance(tg, (bool, int)) else False
    for key in ("alert_start_hour", "alert_end_hour"):
        try:
            hh = int(data.get(key, out[key]))
        except (TypeError, ValueError):
            hh = out[key]
        out[key] = min(max(hh, 0), 23)
    try:
        ac = float(data.get("alert_cooldown_sec", out["alert_cooldown_sec"]))
    except (TypeError, ValueError):
        ac = out["alert_cooldown_sec"]
    out["alert_cooldown_sec"] = min(max(ac, 30.0), 3600.0)
    return out


def _validate_settings(data):
    """Returns (cleaned_dict, None) or (None, error_string)."""
    if not isinstance(data, dict):
        return None, "body must be a JSON object"
    cleaned = dict(DEFAULT_SETTINGS)
    if "facewatch_enabled" in data:
        en = data["facewatch_enabled"]
        if isinstance(en, bool):
            cleaned["facewatch_enabled"] = en
        elif en in (0, 1):
            cleaned["facewatch_enabled"] = bool(en)
        else:
            return None, "facewatch_enabled must be true/false"
    if "facewatch_interval_sec" in data:
        try:
            iv = float(data["facewatch_interval_sec"])
        except (TypeError, ValueError):
            return None, "facewatch_interval_sec must be a number"
        if not 2.0 <= iv <= 300.0:
            return None, "facewatch_interval_sec must be 2..300"
        cleaned["facewatch_interval_sec"] = iv
    if "motion_thresh" in data:
        try:
            th = float(data["motion_thresh"])
        except (TypeError, ValueError):
            return None, "motion_thresh must be a number"
        if not 1.0 <= th <= 100.0:
            return None, "motion_thresh must be 1..100"
        cleaned["motion_thresh"] = th
    if "night_ir_mode" in data:
        if data["night_ir_mode"] not in ("off", "on"):
            return None, "night_ir_mode must be off/on"
        cleaned["night_ir_mode"] = data["night_ir_mode"]
    if "capture_mode" in data:
        if data["capture_mode"] not in ("shots", "clips", "off"):
            return None, "capture_mode must be shots/clips/off"
        cleaned["capture_mode"] = data["capture_mode"]
    if "capture_trigger" in data:
        if data["capture_trigger"] not in ("motion", "faces", "both"):
            return None, "capture_trigger must be motion/faces/both"
        cleaned["capture_trigger"] = data["capture_trigger"]
    if "clip_sec" in data:
        try:
            cs = float(data["clip_sec"])
        except (TypeError, ValueError):
            return None, "clip_sec must be a number"
        if not 3.0 <= cs <= 60.0:
            return None, "clip_sec must be 3..60"
        cleaned["clip_sec"] = cs
    if "clip_cooldown_sec" in data:
        try:
            cd = float(data["clip_cooldown_sec"])
        except (TypeError, ValueError):
            return None, "clip_cooldown_sec must be a number"
        if not 5.0 <= cd <= 300.0:
            return None, "clip_cooldown_sec must be 5..300"
        cleaned["clip_cooldown_sec"] = cd
    if "telegram_enabled" in data:
        en = data["telegram_enabled"]
        if isinstance(en, bool):
            cleaned["telegram_enabled"] = en
        elif en in (0, 1):
            cleaned["telegram_enabled"] = bool(en)
        else:
            return None, "telegram_enabled must be true/false"
    for key in ("alert_start_hour", "alert_end_hour"):
        if key in data:
            try:
                hh = int(data[key])
            except (TypeError, ValueError):
                return None, f"{key} must be an integer"
            if not 0 <= hh <= 23:
                return None, f"{key} must be 0..23"
            cleaned[key] = hh
    if "alert_cooldown_sec" in data:
        try:
            ac = float(data["alert_cooldown_sec"])
        except (TypeError, ValueError):
            return None, "alert_cooldown_sec must be a number"
        if not 30.0 <= ac <= 3600.0:
            return None, "alert_cooldown_sec must be 30..3600"
        cleaned["alert_cooldown_sec"] = ac
    return cleaned, None


def _save_settings_file(data):
    """Atomic write of the full settings dict. Returns error or None."""
    try:
        os.makedirs(SNAP_DIR, exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except OSError as exc:
        return str(exc)
    return None


def _seed_settings(seed):
    """Write seed merged over defaults, only when no file exists yet.

    Readers clamp on load, so raw CLI values are safe here.
    """
    with _SETTINGS_LOCK:
        if os.path.exists(SETTINGS_PATH):
            return
        merged = dict(DEFAULT_SETTINGS)
        merged.update(seed)
        _save_settings_file(merged)


_cam_lock = threading.Lock()
# Who holds _cam_lock since when (diagnosis for 503s).
_cam_holder = {"what": None, "since": 0.0}


def _cam_acquire(what: str) -> bool:
    ok = _cam_lock.acquire(blocking=False)
    if ok:
        import time as _time

        _cam_holder["what"] = what
        _cam_holder["since"] = _time.monotonic()
    return ok


def _cam_release() -> None:
    _cam_holder["what"] = None
    try:
        _cam_lock.release()
    except RuntimeError:
        pass

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
# G.711 audio chunks (stripped) published by the reader for clip muxing.
_audio_lock = threading.Lock()
_audio_chunks: list = []


def _sd_call(fn):
    """Run fn(v720_ap) on a dedicated SD session.

    Waits briefly for transient holders (snapshot/PTZ); a running live
    viewer holds the lock indefinitely, so callers must pause it first.
    Returns None when busy/failing.
    """
    import time as _time

    from v720_ap import v720_ap

    for _ in range(20):
        if _cam_acquire("sd"):
            break
        _time.sleep(0.5)
    else:
        return None
    try:
        sock = _open_cam(*CAMERA)
        try:
            return fn(v720_ap(sock))
        finally:
            _close_cam(sock)
    except Exception:  # noqa: BLE001
        return None
    finally:
        _cam_release()


def _prune_snapshots(days: float) -> int:
    """Delete SNAP_DIR files older than `days`. Returns count removed."""
    import time as _time

    if days <= 0:
        return 0
    cutoff = _time.time() - days * 86400.0
    removed = 0
    try:
        names = os.listdir(SNAP_DIR)
    except OSError:
        return 0
    for name in names:
        if not _shot_name_ok(name) and not name.endswith(".avi"):
            continue
        path = os.path.join(SNAP_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
                removed += 1
        except OSError:
            pass
    return removed


def _sweep_part_files(snap_dir=None) -> int:
    """Delete orphan `.part-*` mux temp files. Returns count removed."""
    target = snap_dir or SNAP_DIR
    removed = 0
    try:
        names = os.listdir(target)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(".part-"):
            continue
        try:
            os.unlink(os.path.join(target, name))
            removed += 1
        except OSError:
            pass
    return removed


def _retention_worker(days: float) -> None:
    import time as _time

    while True:
        _time.sleep(6 * 3600.0)
        try:
            _prune_snapshots(days)
        except Exception:  # noqa: BLE001
            pass


def _transcode_sd(path: str):
    """Transcode a downloaded SD AVI to browser-playable mp4 next to it.

    Returns the mp4 file name, or None when ffmpeg cannot convert it.
    """
    import subprocess

    if not path.endswith(".avi"):
        return None
    out = path[:-len(".avi")] + ".mp4"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", path,
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-movflags", "+faststart", out],
            timeout=300, capture_output=True)
    except (OSError, ValueError):
        return None
    if proc.returncode != 0 or not os.path.exists(out):
        return None
    return os.path.basename(out)


def _shot_thumb(name: str):
    """First frame of an AVI as JPEG bytes, cached next to it. None if off."""
    import subprocess

    if not name.endswith(".avi") or not _shot_name_ok(name):
        return None
    path = os.path.join(SNAP_DIR, name)
    thumb = path + ".thumb.jpg"
    if os.path.exists(thumb):
        try:
            with open(thumb, "rb") as fh:
                return fh.read()
        except OSError:
            return None
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", path,
             "-vframes", "1", thumb],
            timeout=20, capture_output=True)
    except (OSError, ValueError):
        return None
    if proc.returncode != 0 or not os.path.exists(thumb):
        return None
    try:
        with open(thumb, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _shot_name_ok(name: str) -> bool:
    """Allow only our own snapshot/clip file names (no traversal)."""
    import re

    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.(jpg|mp4|avi)",
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
        m = re.fullmatch(r"([A-Za-z0-9-]+)-(\d{8})-(\d{6})((?:-\d+|-part|-face|-manual|-auto)*)\.(jpg|mp4|avi)",
                          name)
        if not m:
            continue
        if day and m.group(2) != day:
            continue
        t = m.group(3)
        ext = m.group(5)
        infixes = m.group(4) or ""
        face = "-face" in infixes
        if face or "-auto" in infixes:
            src = "auto"
        elif "-manual" in infixes or (not infixes and ext == "jpg"):
            src = "manual"
        else:
            src = None
        try:
            size = os.path.getsize(os.path.join(SNAP_DIR, name))
        except OSError:
            size = -1
        out.append({"name": name, "day": m.group(2),
                    "time": f"{t[0:2]}:{t[2:4]}:{t[4:6]}",
                    "kind": "video" if ext == "mp4" else
                            "file" if ext == "avi" else "shot",
                    "face": face,
                    "src": src,
                    "bytes": size})
    return out


def _grab_frame():
    """Single capture attempt, None when busy/failing. For periodic worker."""
    if not _cam_acquire("periodic"):
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
        _cam_release()


def _detect_faces(img: bytes):
    """Face count via YuNet, None when the model is unavailable.

    Model auto-downloads once (~300KB) to FACE_MODEL or ~/.cache/yunet.onnx.
    """
    import os as _os

    try:
        import cv2 as _cv2
        import numpy as _np
    except ImportError:
        return None
    model = _os.environ.get(
        "FACE_MODEL", _os.path.expanduser("~/.cache/yunet.onnx"))
    if not _os.path.exists(model):
        try:
            import urllib.request as _url

            _os.makedirs(_os.path.dirname(model) or ".", exist_ok=True)
            _url.urlretrieve(
                "https://github.com/opencv/opencv_zoo/raw/main/models/"
                "face_detection_yunet/face_detection_yunet_2023mar.onnx",
                model)
        except OSError:
            return None
    try:
        frame = _cv2.imdecode(
            _np.frombuffer(img, dtype=_np.uint8), _cv2.IMREAD_COLOR)
        if frame is None:
            return 0
        h, w = frame.shape[:2]
        det = _cv2.FaceDetectorYN_create(
            model, "", (w, h), 0.6, 0.3, 5000)
        ok, faces = det.detect(frame)
        return 0 if faces is None else len(faces)
    except Exception:  # noqa: BLE001 - cv2 errors vary by build
        return None


BURST_N = 3


def _pick_best(faces_list, motion_list):
    """Best burst frame index: most faces, tie-break higher motion.

    Pure helper so the ranking policy is unit-testable without a camera.
    """
    best = 0
    for i in range(1, len(faces_list)):
        if (faces_list[i], motion_list[i]) > (faces_list[best], motion_list[best]):
            best = i
    return best


def _facewatch_worker():
    """Event capture: shots or clips on motion/faces, per settings. Daemon.

    Reads settings every loop (no restart needed): mode (shots/clips/off),
    trigger (motion/faces/both), interval/threshold and clip
    seconds/cooldown apply on the next cycle. Shots mode keeps the
    burst best-frame behavior; clips mode records a background clip per
    trigger event instead of saving shots (no pre-roll: the clip starts
    at the trigger). Skips while the camera is busy, so it never fights
    live viewing (documented limitation)."""
    import time as _time

    try:
        import numpy as _np
        import cv2 as _cv2
    except ImportError:
        return

    def small(img: bytes):
        arr = _np.frombuffer(img, dtype=_np.uint8)
        frame = _cv2.imdecode(arr, _cv2.IMREAD_GRAYSCALE)
        if frame is None:
            return None
        return _cv2.resize(frame, (160, 120))

    prev = None
    last_mode = last_trigger = None
    last_clip = 0.0
    while True:
        try:
            cfg = _load_settings()
            mode = cfg["capture_mode"]
            trigger = cfg["capture_trigger"]
            if (mode, trigger) != (last_mode, last_trigger):
                print(f"facewatch: mode={mode} trigger={trigger} "
                      f"(interval={cfg['facewatch_interval_sec']} "
                      f"thresh={cfg['motion_thresh']})", flush=True)
                last_mode, last_trigger = mode, trigger
            if not cfg["facewatch_enabled"] or mode == "off":
                prev = None
                _time.sleep(2.0)
                continue
            interval = cfg["facewatch_interval_sec"]
            thresh = cfg["motion_thresh"]
            # Wait out the interval in short slices so a disable
            # applies fast.
            due = _time.monotonic() + interval
            while _time.monotonic() < due:
                _time.sleep(0.5)
                now_cfg = _load_settings()
                if not now_cfg.get("facewatch_enabled", True) or \
                        now_cfg.get("capture_mode", "shots") == "off":
                    prev = None
                    due = None
                    break
            if due is None:
                continue
            try:
                img = _grab_frame()
            except Exception:  # noqa: BLE001
                print("facewatch: grab raised, skipping", flush=True)
                continue
            if img is None:
                print("facewatch: camera busy, skipping", flush=True)
                continue
            cur = small(img)
            if cur is None:
                print("facewatch: undecodable frame, skipping", flush=True)
                continue
            want_faces = trigger in ("faces", "both")
            want_motion = trigger in ("motion", "both")
            faces = (_detect_faces(img) or 0) if want_faces else 0
            moved = False
            motion = 0.0
            if prev is not None:
                import numpy as _np2

                motion = float(_np2.mean(_cv2.absdiff(cur, prev)))
                prev = cur
                moved = motion >= thresh
            else:
                prev = cur
                print("facewatch: first frame, arming", flush=True)
                continue
            event = (moved and want_motion) or (faces > 0 and want_faces)
            if not event:
                print(f"facewatch: motion={motion:.1f} faces={faces} "
                      f"(thresh={thresh}) discard", flush=True)
                continue
            if mode == "clips":
                # No shots in clips mode: record the window instead.
                # Cooldown keeps consecutive events to one clip.
                now = _time.monotonic()
                if now - last_clip < cfg["clip_cooldown_sec"]:
                    print("facewatch: clip cooldown, skipping", flush=True)
                    continue
                last_clip = now
                tag = "auto-face" if faces > 0 else "auto"
                th = threading.Thread(target=_auto_clip,
                                      args=(cfg["clip_sec"], tag),
                                      daemon=True)
                th.start()
                print(f"facewatch: clip started ({tag}, "
                      f"{cfg['clip_sec']}s)", flush=True)
                _maybe_alert(img, motion, faces)
                continue
            if moved and want_motion:
                # Burst on motion-hit: grab fast follow-ups, keep the best
                # single frame. Abort on busy/undecodable, keeping partial.
                cand_imgs = [img]
                cand_faces = [faces]
                cand_motion = [motion]
                last_small = cur
                for _ in range(BURST_N - 1):
                    try:
                        bimg = _grab_frame()
                    except Exception:  # noqa: BLE001
                        print("facewatch: burst grab raised, "
                              "keeping partial", flush=True)
                        break
                    if bimg is None:
                        print("facewatch: burst camera busy, "
                              "keeping partial", flush=True)
                        break
                    bcur = small(bimg)
                    if bcur is None:
                        print("facewatch: burst undecodable frame, "
                              "skipping", flush=True)
                        continue
                    import numpy as _np3

                    bmotion = float(
                        _np3.mean(_cv2.absdiff(bcur, last_small)))
                    cand_imgs.append(bimg)
                    cand_faces.append((_detect_faces(bimg) or 0)
                                     if want_faces else 0)
                    cand_motion.append(bmotion)
                    last_small = bcur
                best = _pick_best(cand_faces, cand_motion)
                # Anti-cluster: reference the burst end so the next cycle
                # measures change since this event, not inside it. The
                # interval timer restarts from here: the next due is
                # computed fresh at the top of the loop.
                prev = last_small
                bfaces = cand_faces[best]
                bmotion = cand_motion[best]
                try:
                    name = _save_snapshot(cand_imgs[best], src="auto")
                except OSError:
                    print("facewatch: save failed", flush=True)
                    continue
                try:
                    if bfaces > 0:
                        base, dot, ext = name.rpartition(".")
                        os.rename(os.path.join(SNAP_DIR, name),
                                  os.path.join(SNAP_DIR, base + "-face." + ext))
                        name = base + "-face." + ext
                except OSError:
                    pass
                print(f"facewatch: burst saved {name} "
                      f"(frames={len(cand_imgs)} best={best} "
                      f"motion={bmotion:.1f} faces={bfaces})", flush=True)
                _maybe_alert(cand_imgs[best], bmotion, bfaces)
                continue
            try:
                name = _save_snapshot(img, src="auto")
            except OSError:
                print("facewatch: save failed", flush=True)
                continue
            try:
                if faces > 0:
                    base, dot, ext = name.rpartition(".")
                    os.rename(os.path.join(SNAP_DIR, name),
                              os.path.join(SNAP_DIR, base + "-face." + ext))
                    name = base + "-face." + ext
            except OSError:
                pass
            print(f"facewatch: saved {name} "
                  f"(motion={motion:.1f} faces={faces})", flush=True)
            _maybe_alert(img, motion, faces)
        except Exception as exc:  # noqa: BLE001 - never let the thread die
            print(f"facewatch: loop error: {type(exc).__name__}: {exc}",
                  flush=True)
            prev = None
            _time.sleep(5.0)


def _periodic_worker(interval: float):
    """Save a frame every interval seconds; skip when busy. Daemon.

    Tags portraits: snapshots with faces are renamed with a -face infix
    so the gallery can badge them.
    """
    import time as _time

    while True:
        _time.sleep(interval)
        try:
            img = _grab_frame()
        except Exception:  # noqa: BLE001
            continue
        if img is None:
            continue
        try:
            name = _save_snapshot(img, src="auto")
        except OSError:
            continue
        try:
            if (_detect_faces(img) or 0) > 0:
                base, dot, ext = name.rpartition(".")
                os.rename(os.path.join(SNAP_DIR, name),
                          os.path.join(SNAP_DIR, base + "-face." + ext))
        except OSError:
            pass


def _record_clip(seconds: float, tag: str | None = None):
    """Capture `seconds` of live JPEGs and mux to timestamped .mp4.

    Returns the file name, or None when no frames arrived. Needs
    numpy+opencv (requirements-min); raises RuntimeError without them.
    Caller must hold _cam_lock. `tag` marks automatic clips
    ("auto", "auto-face") so the gallery can badge them; manual
    recordings pass None.
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
    infix = f"-{tag}" if tag in ("auto", "auto-face") else ""
    name = f"{UID}-{stamp}{infix}.mp4"
    path = os.path.join(SNAP_DIR, name)
    n = 1
    while os.path.exists(path):
        n += 1
        name = f"{UID}-{stamp}-{n}.mp4"
        path = os.path.join(SNAP_DIR, name)
    tmp = os.path.join(SNAP_DIR, ".part-" + name)  # keep .mp4 suffix
    elapsed = _time.monotonic() - t0
    fps = min(max(len(dec) / max(elapsed, 0.1), 1.0), 15.0)
    with _audio_lock:
        audio = b"".join(_audio_chunks)
        del _audio_chunks[:]
    if not _mux_ffmpeg(dec, tmp, w, h, fps, audio or None):
        _mux_cv2(dec, tmp, w, h)
    os.rename(tmp, path)
    return name


def _auto_clip(seconds: float, tag: str):
    """Background auto-clip for the facewatch worker. Daemon-safe.

    Non-blocking camera acquire: skips with a log line when busy so
    the worker loop (and live viewing) never stalls on a recording.
    """
    if not _cam_acquire("auto-clip"):
        print("facewatch: auto-clip skipped (camera busy)", flush=True)
        return
    try:
        try:
            name = _record_clip(seconds, tag=tag)
        except RuntimeError as exc:
            print(f"facewatch: auto-clip failed: {exc}", flush=True)
            return
        except Exception as exc:  # noqa: BLE001
            print(f"facewatch: auto-clip error: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            return
    finally:
        _cam_release()
    if name is None:
        print("facewatch: auto-clip got no frames", flush=True)
        return
    print(f"facewatch: auto-clip saved {name}", flush=True)


def _cordoba_now():
    """Current wall time in America/Argentina/Cordoba.

    Falls back to fixed UTC-3 (Cordoba has no DST) when the tz database
    is unavailable, e.g. slim containers without tzdata.
    """
    import datetime as _dt

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Argentina/Cordoba")
    except Exception:  # noqa: BLE001
        tz = _dt.timezone(_dt.timedelta(hours=-3), "ART")
    return _dt.datetime.now(tz)


def _hour_in_window(hour, start, end):
    """Pure schedule predicate; overnight wrap supported.

    Start inclusive, end exclusive; start == end means always on.
    """
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _cooldown_ok(now_mono, last_sent_mono, cooldown_sec):
    """Pure cooldown gate; first event (no prior send) always passes."""
    if last_sent_mono is None:
        return True
    return (now_mono - last_sent_mono) >= cooldown_sec


def _alert_decision(enabled, hour, start, end, now_mono, last_sent_mono,
                    cooldown_sec):
    """Pure notifier decision: (send: bool, reason: str)."""
    if not enabled:
        return False, "disabled"
    if not _hour_in_window(hour, start, end):
        return False, "out-of-window"
    if not _cooldown_ok(now_mono, last_sent_mono, cooldown_sec):
        return False, "cooldown"
    return True, "send"


def _telegram_send(token, chat_id, jpeg_bytes, caption, timeout=20.0):
    """Send one photo via Bot API sendPhoto. Returns (ok, error)."""
    import json as _json
    import urllib.request as _req

    boundary = "v720alertboundary"
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
            f"{chat_id}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n'
            f"{caption}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; '
            f'filename="alert.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    req = _req.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=head + bytes(jpeg_bytes) + tail,
        headers={"Content-Type":
                 f"multipart/form-data; boundary={boundary}"},
        method="POST")
    try:
        with _req.urlopen(req, timeout=timeout) as resp:
            body = _json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 - network errors vary
        return False, f"{type(exc).__name__}: {exc}"
    if body.get("ok"):
        return True, ""
    return False, str(body.get("description") or body)


def _deliver_alert(token, chat_id, jpeg_bytes, caption, sender=None,
                   delays=(2.0, 5.0)):
    """Up to 3 attempts with backoff, then drop. Returns True if sent."""
    send = sender or _telegram_send
    err = ""
    for attempt in range(1 + len(delays)):
        ok, err = send(token, chat_id, jpeg_bytes, caption)
        if ok:
            return True
        if attempt < len(delays):
            import time as _time

            _time.sleep(delays[attempt])
    print(f"telegram: dropped after retries ({err})", flush=True)
    return False


_alert_state = {"last_sent": None}
_alert_lock = threading.Lock()


def _maybe_alert(jpeg_bytes, motion, faces):
    """Hook called from the facewatch worker on trigger events.

    Gates schedule/cooldown synchronously (cheap), delivers in a
    background thread so the worker loop never stalls on HTTPS.
    Secrets come from the environment at event time, never from git.
    """
    import time as _time

    cfg = _load_settings()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not cfg["telegram_enabled"]:
        return
    if not token or not chat_id:
        print("telegram: missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID",
              flush=True)
        return
    now_mono = _time.monotonic()
    now_local = _cordoba_now()
    with _alert_lock:
        send, reason = _alert_decision(
            True, now_local.hour, cfg["alert_start_hour"],
            cfg["alert_end_hour"], now_mono, _alert_state["last_sent"],
            cfg["alert_cooldown_sec"])
        if send:
            _alert_state["last_sent"] = now_mono
    if not send:
        print(f"telegram: skip ({reason})", flush=True)
        return
    caption = (f"Movimiento {now_local.strftime('%H:%M')} "
               f"(motion {motion:.1f}, caras {faces})")
    th = threading.Thread(target=_deliver_alert,
                          args=(token, chat_id, bytes(jpeg_bytes), caption),
                          daemon=True)
    th.start()
    print(f"telegram: sending ({caption})", flush=True)


def _mux_ffmpeg(dec, tmp, w, h, fps, audio=None) -> bool:
    """H.264 mp4 via ffmpeg pipe (plays inline in browsers).

    Audio is G.711 A-law 8 kHz mono when the camera pushed any.
    """
    import shutil
    import subprocess
    import tempfile

    if shutil.which("ffmpeg") is None:
        return False
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{w}x{h}", "-r", f"{fps:.2f}", "-i", "-"]
    audio_tmp = None
    if audio:
        try:
            with tempfile.NamedTemporaryFile(suffix=".pcm",
                                             delete=False) as af:
                af.write(audio)
                audio_tmp = af.name
            cmd += ["-f", "s16le", "-ar", "8000", "-ac", "1",
                    "-i", audio_tmp]
        except OSError:
            audio_tmp = None
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart"]
    if audio_tmp is not None:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd.append(tmp)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    except OSError:
        return False
    try:
        for d in dec:
            if d.shape[:2] == (h, w):
                proc.stdin.write(d.tobytes())
        proc.stdin.close()
        ok = proc.wait(timeout=30) == 0 and os.path.exists(tmp)
    except (OSError, ValueError):
        try:
            proc.kill()
        except OSError:
            pass
        ok = False
    finally:
        if audio_tmp is not None:
            try:
                os.unlink(audio_tmp)
            except OSError:
                pass
    return ok


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


def _save_snapshot(img: bytes, src: str | None = None) -> str:
    """Store img timestamped under SNAP_DIR, return the file name.

    src tags the origin in the file name: manual (button), auto
    (periodic worker), or None (legacy). Face hits get -face on rename.
    """
    import datetime as _dt

    os.makedirs(SNAP_DIR, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    infix = f"-{src}" if src in ("manual", "auto") else ""
    name = f"{UID}-{stamp}{infix}.jpg"
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
        with _audio_lock:
            del _audio_chunks[:]
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
            if p.cmd == cmd_udp.P2P_UDP_CMD_G711 and \
                    p.msg_flag == cmd_udp.PROTOCOL_MSG_FLAG_FINISH:
                with _audio_lock:
                    _audio_chunks.append(bytes(p.payload[:-5]))
                continue
            if p.cmd == cmd_udp.P2P_UDP_CMD_PCM:
                # This camera pushes audio as raw PCM frames, not G711.
                with _audio_lock:
                    _audio_chunks.append(bytes(p.payload))
                continue
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
            import time as _time

            busy = _cam_holder["what"]
            body = json.dumps(
                [{"uid": UID, "host": CAMERA[0], "port": CAMERA[1],
                  "busy": busy,
                  "busy_secs": round(_time.monotonic() - _cam_holder["since"])
                  if busy else 0}]
            ).encode()
            self._send(200, "application/json", len(body),
                       [("Connection", "close")])
            self.wfile.write(body)
        elif route == f"/dev/{UID}/snapshot":
            self._snapshot()
        elif route == f"/dev/{UID}/live":
            self._live()
        elif route == "/dev/sd/dates":
            dates = _sd_call(lambda cam: cam.sdcard_datelist())
            if dates is None:
                self._send(503, "text/plain", 11,
                           [("Connection", "close")])
                self.wfile.write(b"camera busy")
                return
            body = json.dumps({"dates": dates or []}).encode()
            self._send(200, "application/json", len(body),
                       [("Connection", "close")])
            self.wfile.write(body)
        elif route == "/dev/sd/files":
            query = parse_qs(parts.query)
            date = query.get("date", [None])[0]
            if not date or not date.isdigit():
                self._send(400, "text/plain", 8,
                           [("Connection", "close")])
                self.wfile.write(b"bad date")
                return
            files = _sd_call(
                lambda cam, _d=int(date): [
                    {"hours": h, "minute": m}
                    for (h, m) in (cam.filename_list(_d) or [])])
            if files is None:
                self._send(503, "text/plain", 11,
                           [("Connection", "close")])
                self.wfile.write(b"camera busy")
                return
            body = json.dumps({"date": date, "files": files}).encode()
            self._send(200, "application/json", len(body),
                       [("Connection", "close")])
            self.wfile.write(body)
        elif route == "/dev/shots":
            query = parse_qs(parts.query)
            body = json.dumps(
                _list_shots(query.get("day", [None])[0])).encode()
            self._send(200, "application/json", len(body),
                       [("Connection", "close")])
            self.wfile.write(body)
        elif route == "/dev/settings":
            body = json.dumps(_load_settings()).encode()
            self._send(200, "application/json", len(body),
                       [("Connection", "close")])
            self.wfile.write(body)
        elif route.startswith("/dev/shots/"):
            name = route[len("/dev/shots/"):]
            if name.endswith("/thumb"):
                img = _shot_thumb(name[:-len("/thumb")])
                if img is None:
                    self._send(404, "text/plain", 9,
                               [("Connection", "close")])
                    self.wfile.write(b"not found")
                    return
                self._send(200, "image/jpeg", len(img),
                           [("Connection", "close")])
                self.wfile.write(img)
                return
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
            ctype = ("video/mp4" if name.endswith(".mp4") else
                     "video/x-msvideo" if name.endswith(".avi") else
                     "image/jpeg")
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
        if route == f"/dev/{UID}/ir":
            self._ir(query)
            return
        if route == "/dev/settings":
            self._settings_save()
            return
        if route == "/dev/sd/download":
            self._sd_download(query)
            return
        if route == "/dev/alerts/test":
            self._alert_test()
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
            if _cam_acquire("clip"):
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
            _cam_release()
        if name is None:
            self._send(502, "text/plain", 17,
                       [("Connection", "close")])
            self.wfile.write(b"no frames in time")
            return
        body = json.dumps({"clip": name, "seconds": seconds}).encode()
        self._send(200, "application/json", len(body),
                   [("Connection", "close")])
        self.wfile.write(body)

    def _alert_test(self) -> None:
        """Send a test alert reusing the notifier send path.

        Uses the newest auto snapshot (or a live grab when idle).
        Reports the schedule/cooldown decision without changing settings
        or the cooldown state. Never echoes secrets.
        """
        import glob as _glob
        import time as _time

        def reply(code, obj):
            body = json.dumps(obj).encode()
            self._send(code, "application/json", len(body),
                       [("Connection", "close")])
            self.wfile.write(body)

        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            reply(500, {"sent": False,
                        "error": "missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID"})
            return
        cands = sorted(_glob.glob(os.path.join(SNAP_DIR, "*-auto*.jpg")),
                       key=os.path.getmtime, reverse=True)
        jpeg = None
        if cands:
            try:
                with open(cands[0], "rb") as fh:
                    jpeg = fh.read()
            except OSError:
                jpeg = None
        if jpeg is None:
            # The worker usually holds the lock (2s grabs); wait for a
            # gap like the manual clip endpoint does.
            for _ in range(20):
                try:
                    jpeg = _grab_frame()
                except Exception:  # noqa: BLE001
                    jpeg = None
                if jpeg is not None:
                    break
                _time.sleep(0.5)
            if jpeg is None:
                reply(503, {"sent": False,
                            "error": "no snapshot and camera busy"})
                return
        cfg = _load_settings()
        now_local = _cordoba_now()
        with _alert_lock:
            _, reason = _alert_decision(
                cfg["telegram_enabled"], now_local.hour,
                cfg["alert_start_hour"], cfg["alert_end_hour"],
                _time.monotonic(), _alert_state["last_sent"],
                cfg["alert_cooldown_sec"])
        caption = f"Prueba {now_local.strftime('%H:%M')} (bot ok)"
        ok, err = _telegram_send(token, chat_id, jpeg, caption)
        if not ok:
            reply(502, {"sent": False, "error": err,
                        "decision": reason})
            return
        reply(200, {"sent": True, "decision": reason})

    def _sd_download(self, query) -> None:
        """Download one SD minute-file to the Pi (pauses camera recording
        while transferring, per firmware behavior)."""
        try:
            date = int(query.get("date", ["0"])[0])
            hours = int(query.get("hours", ["-1"])[0])
            minute = int(query.get("minute", ["-1"])[0])
        except ValueError:
            date, hours, minute = 0, -1, -1
        if date <= 0 or not 0 <= hours <= 23 or not 0 <= minute <= 59:
            self._send(400, "text/plain", 11,
                       [("Connection", "close")])
            self.wfile.write(b"bad params")
            return

        def _fetch(cam):
            info = cam.avi_file_info(date, hours, minute) or {}
            want = info.get("fileSize", -1)
            info_data, data = cam.get_file(date, hours, minute) or (None, None)
            if data is None:
                return ("error", "empty transfer", 0, want)
            base = f"{UID}-sd-{date:08d}-{hours:02d}{minute:02d}00"
            if want and want > 0 and len(data) < want:
                if len(data) < 262144:
                    # Too short to be useful: report, don't keep a stub.
                    return ("short", len(data), want)
                name = base + "-part.avi"  # firmware wall ~1MB, keep prefix
            else:
                name = base + ".avi"
            path = os.path.join(SNAP_DIR, name)
            os.makedirs(SNAP_DIR, exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)
            mp4 = _transcode_sd(path)
            if mp4 is not None:
                try:
                    os.unlink(path)  # gallery keeps the playable mp4 only
                except OSError:
                    pass
            return {"file": mp4 or name, "bytes": len(data), "mp4": mp4}

        res = _sd_call(_fetch)
        if res is None:
            self._send(503, "text/plain", 11,
                       [("Connection", "close")])
            self.wfile.write(b"camera busy")
            return
        if isinstance(res, tuple):
            kind, got, want = res[0], res[1], res[2] if len(res) > 2 else -1
            msg = (f"camera sent {got} of {want} bytes; "
                   f"retry once the live feed is fully stopped"
                   if kind == "short" else f"transfer error: {got}")
            self._send(502, "text/plain", len(msg),
                       [("Connection", "close")])
            self.wfile.write(msg.encode())
            return
        body = json.dumps(res).encode()
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
        if not _cam_acquire("ptz"):
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
            _cam_release()
        body = json.dumps({"moved": direction, "ms": ms,
                           "via": "direct"}).encode()
        self._send(200, "application/json", len(body),
                   [("Connection", "close")])
        self.wfile.write(body)

    def _ir(self, query) -> None:
        """Toggle the IR LED: ?on=0|1. Manual only (#28 phase 1);
        the camera's own ON behavior is already automatic.
        """
        global _live_sock
        from v720_ap import v720_ap

        on = query.get("on", [""])[0]
        if on not in ("0", "1"):
            self._send(400, "text/plain", 13,
                       [("Connection", "close")])
            self.wfile.write(b"on must be 0|1")
            return
        ena = on == "1"
        with _live_sock_lock:
            shared = _live_sock
        if shared is not None:
            try:
                from prot_ap import prot_ap
                import cmd_udp

                _send_pkt(shared, prot_ap(content={
                    "code": cmd_udp.CODE_FORWARD_DEV_IR_LED,
                    "devTarget": "deadbeef",
                    "IrLed": 1 if ena else 0,
                }).req())
            except Exception as exc:  # noqa: BLE001 - stale: fall through
                log("ap-gateway").warn("ir: shared socket dead (%s)", exc)
                with _live_sock_lock:
                    if _live_sock is shared:
                        _live_sock = None
                shared = None
            else:
                body = json.dumps({"ir": 1 if ena else 0,
                                   "via": "live"}).encode()
                self._send(200, "application/json", len(body),
                           [("Connection", "close")])
                self.wfile.write(body)
                return
        if not _cam_acquire("ir"):
            self._send(503, "text/plain", 11,
                       [("Connection", "close")])
            self.wfile.write(b"camera busy")
            return
        try:
            sock = _open_cam(*CAMERA)
            try:
                resp = v720_ap(sock).ir_led(ena)
            finally:
                _close_cam(sock)
        except Exception as exc:  # noqa: BLE001
            self._send(502, "text/plain", len(str(exc)),
                       [("Connection", "close")])
            self.wfile.write(str(exc).encode())
            return
        finally:
            _cam_release()
        body = json.dumps({"ir": 1 if ena else 0,
                           "via": "direct",
                           "ack": bool(resp)}).encode()
        self._send(200, "application/json", len(body),
                   [("Connection", "close")])
        self.wfile.write(body)

    def _settings_save(self) -> None:
        """Validate + persist settings JSON. Unknown keys are ignored."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 4096:
            self._send(400, "text/plain", 10,
                       [("Connection", "close")])
            self.wfile.write(b"bad length")
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, "text/plain", 8,
                       [("Connection", "close")])
            self.wfile.write(b"bad json")
            return
        current = _load_settings()
        merged = dict(current)
        for key in DEFAULT_SETTINGS:
            if key in data:
                merged[key] = data[key]
        cleaned, err = _validate_settings(merged)
        if err is not None:
            self._send(400, "text/plain", len(err),
                       [("Connection", "close")])
            self.wfile.write(err.encode())
            return
        with _SETTINGS_LOCK:
            werr = _save_settings_file(cleaned)
        if werr is not None:
            self._send(500, "text/plain", len(werr),
                       [("Connection", "close")])
            self.wfile.write(werr.encode())
            return
        body = json.dumps(cleaned).encode()
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
        if not _cam_acquire("snapshot"):
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
            _cam_release()

    def _serve_snapshot(self, img: bytes, save: bool):
        if save:
            name = _save_snapshot(img, src="manual")
            body = json.dumps({"saved": name}).encode()
            self._send(200, "application/json", len(body),
                       [("Connection", "close")])
            self.wfile.write(body)
            return
        self._send(200, "image/jpeg", len(img),
                   [("Connection", "close")])
        self.wfile.write(img)

    def _live(self):
        if not _cam_acquire("live"):
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
            _cam_release()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera", default="192.168.169.1:6123")
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--snap-every", type=float,
                    default=float(os.environ.get("SNAP_EVERY_SEC", "0")),
                    help="periodic snapshot interval in seconds, 0 disables")
    ap.add_argument("--facewatch-every", type=float,
                    default=float(os.environ.get("FACE_WATCH_SEC", "0")),
                    help="event capture interval in seconds (motion+faces only), 0 disables")
    ap.add_argument("--retain-days", type=float,
                    default=float(os.environ.get("SNAP_RETENTION_DAYS", "0")),
                    help="delete local snapshots/clips older than N days, 0 disables")
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
    # Seed the settings file from CLI/env only on first boot; afterwards
    # the file (edited via the page) is the source of truth.
    seed = {}
    if args.facewatch_every > 0:
        seed["facewatch_interval_sec"] = args.facewatch_every
    _seed_settings(seed)
    th = threading.Thread(target=_facewatch_worker, daemon=True)
    th.start()
    print(f"facewatch worker started (governed by {SETTINGS_PATH})")
    swept = _sweep_part_files()
    if swept:
        print(f"startup: removed {swept} orphan .part files", flush=True)
    if args.retain_days > 0:
        n = _prune_snapshots(args.retain_days)
        print(f"retention: removed {n} files older than {args.retain_days}d")
        th = threading.Thread(target=_retention_worker,
                              args=(args.retain_days,), daemon=True)
        th.start()

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

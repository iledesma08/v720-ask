#!/usr/bin/env python3
"""Re-probe parked opcodes 209 (SD record toggle) and 412 (SD delete).

Usage (from repo root, Pi with wlan0 -> Nax_*, GATEWAY STOPPED — single
session camera):
    PYTHONPATH=src /tmp/v720fp/bin/python scripts/probe_parked.py --op 209
    PYTHONPATH=src /tmp/v720fp/bin/python scripts/probe_parked.py --op 412 \
        --date 20260907 --hours 16 --minute 56 --i-have-backup

Safety:
- 209 is reversible (the script re-enables recording before exit).
- 412 needs --i-have-backup: download the target minute via the gallery
  FIRST (gateway running), then stop the gateway and probe. The script
  re-lists afterwards and reports DELETED vs PRESENT.
- Always blind-closes the session (see #10). On wedge/beeps: physical
  reset, same as ever.

Exit 0 with a VERDICT line in all cases; exit 2 on transport failure.
"""
import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from log import log  # noqa: E402


def _init_cam(netcl_tcp, v720_ap, host, port, attempts=3):
    last = None
    for i in range(1, attempts + 1):
        try:
            sock = netcl_tcp(host, port)
            sock.open()
            cam = v720_ap(sock)
            cam.init_live_motion()
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


def _blind_close(sock):
    from prot_ap import prot_ap

    try:
        sock.send(prot_ap(content={"code": 0,
                                   "devTarget": "deadbeef"}).req())
    except Exception:  # noqa: BLE001
        pass
    try:
        sock.close()
    except Exception:  # noqa: BLE001
        pass


def _show(label, resp):
    if resp is None:
        print(f"{label}: TIMEOUT (no response)")
        return None
    if isinstance(resp, dict):
        content = resp.get("content", resp)
    else:
        content = getattr(resp, "content", resp)
    print(f"{label}: {content!r}")
    return content


def op_209(cam):
    import cmd_udp
    import prot_json_udp

    print("209: sdCardReco=0 (stop SD recording)")
    r0 = cam._ap_req({
        "code": 209,
        "devTarget": prot_json_udp.DEFAULT_DEV_TARGET,
        "sdCardReco": 0,
    })
    c0 = _show("209-stop", r0)
    print("209: sdCardReco=1 (RESTORE recording)")
    r1 = cam._ap_req({
        "code": 209,
        "devTarget": prot_json_udp.DEFAULT_DEV_TARGET,
        "sdCardReco": 1,
    })
    c1 = _show("209-start", r1)
    if c0 is None and c1 is None:
        print("VERDICT-209: TIMEOUT (stays parked)")
    else:
        print("VERDICT-209: ANSWERED (un-park, wire it up)")


def op_412(cam, date, hours, minute):
    import cmd_udp
    import prot_json_udp

    before = cam.filename_list(date) or []
    print(f"412: files on {date} before: {before}")
    target = (hours, minute) not in [tuple(x) for x in before]
    if target:
        print("412: target minute not on SD, nothing to delete. "
              "Pick --date/--hours/--minute from the list above.")
        print("VERDICT-412: NO-TARGET")
        return
    print(f"412: delete date={date} hours={hours} minute={minute}")
    r = cam._ap_req({
        "code": cmd_udp.CODE_SDCARD_REQ_MEDIA_DELETE,
        "devTarget": prot_json_udp.DEFAULT_DEV_TARGET,
        "date": date,
        "hours": hours,
        "minute": minute,
    })
    _show("412-delete", r)
    after = cam.filename_list(date) or []
    print(f"412: files on {date} after: {after}")
    if (hours, minute) not in [tuple(x) for x in after]:
        print("VERDICT-412: DELETED (un-park, wire it up with backup flow)")
    else:
        print("VERDICT-412: PRESENT (stays parked)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.169.1")
    ap.add_argument("--port", type=int, default=6123)
    ap.add_argument("--op", choices=("209", "412"), required=True)
    ap.add_argument("--date", type=int, default=0)
    ap.add_argument("--hours", type=int, default=-1)
    ap.add_argument("--minute", type=int, default=-1)
    ap.add_argument("--i-have-backup", action="store_true")
    args = ap.parse_args()

    if args.op == "412" and not args.i_have_backup:
        print("412 needs --i-have-backup: download the target minute via "
              "the gallery first, then stop the gateway and probe.")
        return 2

    log.set_log_lvl(logging.WARN)

    from netcl_tcp import netcl_tcp
    from v720_ap import v720_ap

    try:
        cam, sock = _init_cam(netcl_tcp, v720_ap, args.host, args.port)
    except Exception as exc:  # noqa: BLE001
        print(f"CONNECT_FAIL: {type(exc).__name__}")
        return 2
    try:
        if args.op == "209":
            op_209(cam)
        else:
            op_412(cam, args.date, args.hours, args.minute)
    finally:
        _blind_close(sock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Join the camera to home WiFi (STA) via opcode 204. EXPERIMENT ONLY.

The password is NEVER stored: pass it via env (STA_WIFI_PASS) or --password.
It appears nowhere else (no logs print it, no files).

Usage (from repo root, Pi with wlan0 -> Nax_*, GATEWAY STOPPED — single
session camera):
    STA_WIFI_PASS='...' PYTHONPATH=src /tmp/v720fp/bin/python \
        scripts/sta_join.py --ssid '<home-ssid-2.4>' --dry-run
    STA_WIFI_PASS='...' PYTHONPATH=src /tmp/v720fp/bin/python \
        scripts/sta_join.py --ssid '<home-ssid-2.4>'

After a successful join the camera leaves the AP: THIS session drops.
That is expected, not a failure. Continue with docs/sta-experiment.md
(lease check, fake-server verification, rollback).

Exit 0 when the command was accepted (or dry run printed); 2 on failure.
"""
import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from log import log  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.169.1")
    ap.add_argument("--port", type=int, default=6123)
    ap.add_argument("--ssid", required=True)
    ap.add_argument("--password", default=os.environ.get("STA_WIFI_PASS", ""))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.password and not args.dry_run:
        print("missing password: --password or STA_WIFI_PASS")
        return 2

    log.set_log_lvl(logging.WARN)

    from netcl_tcp import netcl_tcp
    from prot_ap import prot_ap
    from v720_ap import v720_ap

    envelope = {
        "code": 204,
        "devTarget": "deadbeef",
        "s": args.ssid,
        "p": "***",
    }
    print(f"envelope: {envelope}")
    if args.dry_run:
        print("DRY-RUN: not sending")
        return 0

    sock = None
    try:
        sock = netcl_tcp(args.host, args.port)
        sock.open()
        cam = v720_ap(sock)
        try:
            cam.init_live_motion()
        except Exception as exc:  # noqa: BLE001
            import traceback

            print("INIT-WARN (continuing anyway):")
            traceback.print_exc()
        envelope = {
            "code": 204,
            "devTarget": "deadbeef",
            "s": args.ssid,
            "p": args.password,
        }
        try:
            resp = cam._ap_req(envelope)
            if resp is None:
                print("JOIN-SEND: TIMEOUT (no response; camera may still "
                      "roam — check DHCP, then rollback if silent)")
            else:
                content = getattr(resp, "content", resp)
                print(f"JOIN-SEND: answered {content!r}")
        except KeyError:
            # Some FW answers 204 without a 'content' key: dump raw.
            from prot_ap import prot_ap as _pap
            from prot_json_udp import prot_json_udp as _pju

            raw = cam._req(_pap(content={
                "code": 204,
                "devTarget": "deadbeef",
                "s": args.ssid,
                "p": args.password,
            }).req())
            rj = _pju.resp(raw)
            print(f"JOIN-SEND: answered non-content "
                  f"{rj.json if rj is not None else None!r}")
        print("NOTE: the AP drops from here by design. See "
              "docs/sta-experiment.md next steps.")
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback

        print("JOIN_FAIL:")
        traceback.print_exc()
        return 2
    finally:
        try:
            if sock is not None:
                from prot_ap import prot_ap as _pap2

                try:
                    sock.send(_pap2(content={
                        "code": 0, "devTarget": "deadbeef"}).req())
                except Exception:  # noqa: BLE001
                    pass
                sock.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())

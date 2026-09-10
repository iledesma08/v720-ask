# Web camera passthrough — Pi HOWTO (one page)

Goal: view the camera in the browser via the `:8090` gateway + this page
(`static/index.html`). No gallery or recordings (that goes in #9).

## 1. Requirements

- Pi with free `wlan0` and camera in AP mode (`Nax_*`).
- Gateway: `scripts/ap_gateway.py` (port **8090**, see `docs/gateway-8090.md`).
- Nginx Proxy Manager (NPM) on host network, ports 80/443, as front end.
- The gateway does **not** serve static files: this page must be served from the
  **same origin** as the `/dev/*` proxy (via NPM or a test front end).

## 2. Associate `wlan0` with the camera and verify

```bash
# Associate (adjust interface/SSID for your Pi)
sudo iw dev wlan0 connect 'Nax_XXXX'
ip addr show wlan0            # expect something like 192.168.169.100
nc -vz 192.168.169.1 6123    # the camera should respond
```

If `nc` fails: re-associate `wlan0`, check that no other profile
(NetworkManager) is stealing the interface and retry once.

## 3. Bring up the `:8090` gateway

```bash
PYTHONPATH=src /tmp/v720fp/bin/python scripts/ap_gateway.py --port 8090
curl -s http://127.0.0.1:8090/dev/list   # → [{"uid":"ap-camera",...}]
```

## 4. NPM + AdGuard front end (same recipe validated in `docs/npm-cam-route.md`)

1. AdGuard > DNS rewrites: `camara.lan` → `192.168.0.204`
   (the PC must use AdGuard as DNS; otherwise a hosts entry)
2. NPM Proxy Host `camara.lan` → `192.168.0.204:8090` with Access List
   (auth). No custom locations: `/` (page) and `/dev/*` (API) both
   come from the gateway.

The page uses **relative** URLs (`dev/list`, `dev/<uid>/live`,
`dev/<uid>/snapshot`): it works as long as `/` and `/dev/*` share the
same origin, which is exactly what this recipe sets up.

## 5. Test

```bash
curl -s http://camara.lan/dev/list
curl -o s.jpg http://camara.lan/dev/ap-camera/snapshot && file s.jpg  # → JPEG
```

In the browser (PC on LAN): open `http://camara.lan/` → you see the camera(s)
with a live MJPEG `<img>` + *Watch live* and *Snapshot* links.

## 6. Ports in use on this Pi (do not step on)

| Port      | Service               |
|-----------|-----------------------|
| 80/443    | nginx-proxy-manager   |
| 53        | AdGuard               |
| 8080      | bentopdf              |
| 8081      | metube                |
| 8082      | it-tools              |
| 8083      | stirling              |
| **8090**  | **camera gateway**    |

## 7. Notes

- **One viewer at a time:** a second `/live` responds `503 camera busy`.
  The page reports it and the `img` shows the warning; reload once it is freed.
- Each session closes video (`code 0`) when the client drops (see #10); if you see
  `502 no frame in time`: physical camera reset + one retry.
- **AdGuard (port 53) reserved** for the future STA mode (see #8):
  do not expose or move 53; the web front end stays on NPM (80/443).
- Gallery/recordings: out of scope, tracked in #9.

## 8. Runtime settings (gear in the Camera tab, #27)

- `GET /dev/settings` reads, `POST /dev/settings` validates + persists to
  `snapshots/settings.json` (same volume, survives restarts).
- Keys: `facewatch_enabled` (bool), `facewatch_interval_sec` (2–300),
  `motion_thresh` (1–100), `night_ir_mode` (off/on/auto; auto stored only).
- The facewatch worker re-reads the file every loop — no restart needed.
- Saving `night_ir_mode` on/off also drives the IR LED immediately
  (`POST /dev/ap-camera/ir?on=0|1`, #28 phase 1).
- First boot seeds the file from CLI/env (`FACE_WATCH_SEC`); afterwards the
  file is the source of truth.

# V720 / Naxclow IP cameras — local Raspberry Pi gateway

Use a Naxclow-family Wi-Fi camera (V720 app, `Nax_*` AP) **without the cloud
app**: a Raspberry Pi 5 joins the camera's own Wi-Fi AP and exposes a local
MJPEG page on your LAN. Tested with a PTZ 1080p model, FW `202509111709`,
streaming 640x480 MJPEG + snapshots over `192.168.169.1:6123`.

## What works

- Headless camera check: connect, `baseinfo`, capture JPEGs (`scripts/v720_check.py`)
- MJPEG gateway on LAN port `8090` (`scripts/ap_gateway.py`):
  `GET /dev/list`, `GET /dev/ap-camera/live`, `GET /dev/ap-camera/snapshot`
- Single viewer at a time; every session closes video (`code 0`) on release
- Auto-reconnect with backoff when the camera drops or reboots
- systemd unit + Nginx Proxy Manager route with auth (`http://camara.lan/`)
- No cloud, no app, no DNS hijack needed in AP-direct mode

## Quickstart (Pi 5, camera in AP mode)

```bash
# 1. Join the camera AP with wlan0, keep eth0 on your LAN
iw dev wlan0 link            # expect SSID Nax_*
ping -c3 192.168.169.1 && nc -vz 192.168.169.1 6123

# 2. Minimal venv (pure python, no opencv needed for the gateway)
python3 -m venv /tmp/v720fp && /tmp/v720fp/bin/pip install -r requirements-min.txt

# 3. Check the camera
PYTHONPATH=src /tmp/v720fp/bin/python scripts/v720_check.py --frames 3 --out /tmp
file /tmp/frame*.jpg        # expect 640x480 JPEGs

# 4. Serve it
PYTHONPATH=src /tmp/v720fp/bin/python scripts/ap_gateway.py --port 8090
curl -s http://127.0.0.1:8090/dev/list
```

Full Pi setup (systemd, AdGuard DNS rewrite, NPM with auth, static page):
[`docs/camara-web.md`](docs/camara-web.md) and [`docs/npm-cam-route.md`](docs/npm-cam-route.md).

## Layout

| Path | What |
|---|---|
| `src/` | Original reverse-engineered protocol code, **untouched** |
| `scripts/v720_check.py` | Headless camera check (this fork) |
| `scripts/ap_gateway.py` | AP-direct MJPEG gateway (this fork) |
| `static/index.html` | Minimal same-origin web page |
| `systemd/cam-gateway.service` | systemd unit for the gateway |
| `docs/` | HOWTOs, protocol notes, research |
| `orig-app/` | Original V720 APK + JADX sources (reference only) |
| `requirements-min.txt` | Minimal pins for check + gateway |
| `requirements.txt` | Legacy full stack (opencv viewer) |

## Protocol in 30 seconds

TCP `192.168.169.1:6123` with a 20-byte LE framing header, JSON control
(`501` connect, `502` commands like `4` baseinfo / `3` open video / `0` close)
and fragmented JPEG (`250/251/252`) + G.711 audio. The camera stops pushing
unless the client sends retransmission confirms (`605`) every ~100 ms, and a
live cut without `close` wedges control until a camera reset — the gateway
handles both. Details: [`fake_server.md`](fake_server.md) (STA/cloud mode),
[`docs/`](docs/) (fingerprinting, feasibility).

Known quirks of newer firmware (e.g. `202509111709`): only port `6123` open
(no HTTP/RTSP), Wi-Fi scan (`211`) unanswered, no-SD boot beep-loop until
reset, cold boot beep-loop until reset. STA/cloud fake-server is a
low-priority follow-up, not part of this setup.

## Credits

Reverse engineering, protocol code in `src/`, APK analysis, `fake_server.md`
and the original README: **intx82 and contributors** —
[github.com/intx82/a9-v720](https://github.com/intx82/a9-v720).
This fork adapts that work to a different same-brand PTZ camera and adds the
headless check, the MJPEG gateway, systemd/NPM integration and docs.
Camera hardware and the V720 app belong to their vendor (Naxclow).

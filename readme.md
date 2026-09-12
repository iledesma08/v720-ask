# V720 / Naxclow IP cameras — local Raspberry Pi gateway

Use a Naxclow-family Wi-Fi camera (V720 app, `Nax_*` AP) **without the cloud
app**: a Raspberry Pi 5 joins the camera's own Wi-Fi AP and serves a local
page with live view, snapshots, clips, PTZ and event capture on your LAN.
Tested with a PTZ 1080p model, FW `202509111709`, streaming 640x480 MJPEG +
snapshots over `192.168.169.1:6123`.

## What it does

- **Live + snapshots**: MJPEG live view, one-click JPEG save, 10s manual
  H.264 clips (`scripts/ap_gateway.py` on LAN port `8090`)
- **Event capture** (facewatch): motion gate + YuNet face detection, saving
  **shots** (best-of-burst) or recording **clips** per event, or off —
  trigger on motion, faces, or both, all tunable live from the page
- **Telegram alerts**: motion photo to your phone inside a night window
  (default 00–05 Córdoba) with cooldown anti-spam and a test button
- **Gallery**: shots/clips/SD files with person/automatic/manual badges,
  filters, sort, multi-select download/delete, auto-refresh, inline playback
- **PTZ + night vision**: D-pad controls, manual IR LED
- **SD browser**: list/download minute-files (auto-transcoded to playable mp4)
- **Robustness**: single viewer at a time (every session closes video on
  release), auto-reconnect with backoff, drain-resync against wedged control,
  retransmission confirms every ~100 ms, orphan temp-file sweep on boot
- systemd unit + Nginx Proxy Manager route with auth (e.g. `http://camara.lan/`)
- No cloud, no app, no DNS hijack needed in AP-direct mode

## Deploy (Docker, recommended)

The gateway runs as a container with host networking (it must reach the
camera AP subnet `192.168.169.0/24` through the host's `wlan0`):

```bash
# 1. Join the camera AP with wlan0, keep eth0 on your LAN
iw dev wlan0 link            # expect SSID Nax_*
ping -c3 192.168.169.1 && nc -vz 192.168.169.1 6123

# 2. Optional secrets (Telegram alerts need these, nothing else does)
printf 'TELEGRAM_BOT_TOKEN=<from BotFather>\nTELEGRAM_CHAT_ID=<from getUpdates>\n' > docker/telegram.env
# docker/telegram.env is git-ignored; the stack works without it

# 3. Build + run (code is COPYed into the image: rebuild after every change)
sudo docker compose -f docker-compose.yml build 2>&1 | tail -1
sudo docker compose -f docker-compose.yml up -d
curl -s http://127.0.0.1:8090/dev/list
```

Message your bot once, then get the chat id with:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"
```

Full Pi setup (AdGuard DNS rewrite, NPM with auth): [`docs/camara-web.md`](docs/camara-web.md)
and [`docs/npm-cam-route.md`](docs/npm-cam-route.md).

## Manual run (no Docker)

```bash
python3 -m venv /tmp/v720fp && /tmp/v720fp/bin/pip install -r requirements-min.txt
PYTHONPATH=src /tmp/v720fp/bin/python scripts/v720_check.py --frames 3 --out /tmp
file /tmp/frame*.jpg        # expect 640x480 JPEGs
PYTHONPATH=src /tmp/v720fp/bin/python scripts/ap_gateway.py --port 8090
```

## Settings reference

All live-tunable from the page (⚙), no restart. Persisted to
`snapshots/settings.json`.

| Key | Range | Default | What |
|---|---|---|---|
| `facewatch_enabled` | on/off | on | Master switch for event capture |
| `capture_mode` | shots/clips | shots | Save burst shots, record clips, or nothing (off lives in the switch above) |
| `capture_trigger` | motion/faces/both | both | What counts as an event (motion-only skips face detection entirely) |
| `facewatch_interval_sec` | 2–300 | 20 | Seconds between checks |
| `motion_thresh` | 1–100 | 10 | Lower = more sensitive |
| `clip_sec` | 3–60 | 10 | Seconds per auto-clip (clips mode only) |
| `clip_cooldown_sec` | 5–300 | 30 | Min gap between auto-clips |
| `telegram_enabled` | on/off | off | Phone alerts |
| `alert_start_hour` / `alert_end_hour` | 0–23 | 0–5 | Alert window, Córdoba time, overnight wrap |
| `alert_cooldown_sec` | 30–3600 | 300 | Min gap between alerts |
| `night_ir_mode` | off/on | off | IR LED, manual |

## API cheatsheet

Base `http://<pi>:8090`. Gallery auto-refreshes; all JSON except media.

- `GET /dev/list` — camera presence (also the docker healthcheck; public)
- `GET /dev/ap-camera/live` — MJPEG stream (single viewer; second waits)
- `GET /dev/ap-camera/snapshot?save=1` — JPEG, optionally stored
- `POST /dev/ap-camera/clip?seconds=10` — manual 3–60s H.264 recording
- `GET /dev/shots[?day=YYYYMMDD]` — gallery entries (bytes, badges, filters)
- `GET /dev/shots/<name>` (+ `/thumb`) — file download
- `DELETE /dev/shots/<name>` — delete a file
- `GET/POST /dev/settings` — runtime config JSON
- `POST /dev/ap-camera/ptz?dir=N&ms=M` — D-pad move
- `POST /dev/ap-camera/ir?on=0|1` — IR LED
- `POST /dev/alerts/test` — Telegram test photo (never changes settings)
- `GET /dev/sd/dates`, `/dev/sd/files`, `POST /dev/sd/download` — SD card

## Tests

Pure-function unit tests, no camera or network needed (HTTP is mocked):

```bash
python3 -m unittest discover -s tests -v
```

Covers alert schedule/cooldown/decision/validation/send-retry, boot sweep,
and model-download timeout. Hardware behavior (burst selection, clip mux,
gallery badges) is validated on the Pi per issue acceptance criteria.

## Layout

| Path | What |
|---|---|
| `scripts/ap_gateway.py` | Gateway: HTTP API + workers (this fork) |
| `scripts/v720_check.py` | Headless camera check (this fork) |
| `scripts/face_probe.py` | YuNet probe over snapshots |
| `src/` | Original reverse-engineered protocol code, **untouched** |
| `static/index.html` | Web page (live, gallery, PTZ, settings) |
| `tests/` | Stdlib unit tests |
| `docker/` | Gateway image + compose stack + (git-ignored) secrets env |
| `systemd/` | Alternative non-Docker unit |
| `docs/` | HOWTOs, protocol notes, research, captures |
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
(no HTTP/RTSP), Wi-Fi scan (`211`) unanswered, SD delete (`412`) and
record-toggle (`209`) ineffective, no-SD boot beep-loop until reset, cold
boot beep-loop until reset. STA/cloud fake-server is a low-priority
follow-up, not part of this setup.

## Troubleshooting

- **Beep-loop after (re)boot**: press the physical reset button once; the
  camera then joins normally. A spontaneous reboot was observed once
  (2026-09-11, cause unknown, with SD present) — the loop itself is the
  known quirk, the reboot is not yet explained.
- **Busy / 503s**: single-session camera. Live, clips, grabs and SD
  downloads serialize on one lock; whoever arrives second waits or skips
  with a log line. Close other viewers/tabs first.
- **Live cut wedges control**: the gateway drains and resyncs automatically;
  a manual camera reset is the last resort.
- **Telegram silent**: check the token/chat in `docker/telegram.env`
  (recreate the container after editing), the alert window/cooldown in
  settings, and use the Send-test button — it reports ok/error inline.
- **Gallery empty after auto-capture**: it refreshes every ~10s on its own;
  check filters (All) and the day chips.

## Credits

Reverse engineering, protocol code in `src/`, APK analysis, `fake_server.md`
and the original README: **intx82 and contributors** —
[github.com/intx82/a9-v720](https://github.com/intx82/a9-v720).
This fork adapts that work to a different same-brand PTZ camera and adds the
headless check, the MJPEG gateway, event capture, clips, Telegram alerts,
gallery, PTZ, systemd/Docker integration and docs.
Camera hardware and the V720 app belong to their vendor (Naxclow).

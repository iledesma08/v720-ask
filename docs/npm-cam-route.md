# NPM + AdGuard recipe for camera web (validated 2026-09-07)

Conventions: `<cam-domain>` (e.g. `camara.lan`), `<pi-lan-ip>` (the Pi's
LAN address, e.g. `192.168.0.204`).

Goal: `http://<cam-domain>/` serves `static/index.html` and
`http://<cam-domain>/dev/*` hits the AP-directo gateway (`:8090`),
both behind NPM auth. Gateway owns camera reconnect retries (see #13);
this doc is only the frontal recipe.

## 0. DNS rewrite in AdGuard Home (port 3000)

Filters > DNS rewrites > Add: Domain `<cam-domain>`, Answer `<pi-lan-ip>`.
The browsing PC must use AdGuard as its DNS (else add
`<pi-lan-ip> <cam-domain>` to the PC hosts file). Verify from the PC:
`nslookup <cam-domain>` → `<pi-lan-ip>`.

## 1. Static server for the page (:8000)

```bash
cd <repo>
python3 -m http.server 8000 --directory static
```

(Manual for now; a second systemd unit is future work, not #12.)

## 2. Proxy Host recipe (NPM UI, port 81)

Hosts > Proxy Hosts > Add Proxy Host (edit the existing `<cam-domain>` host):

- Details tab:
  - Domain Names: `<cam-domain>`
  - Scheme: `http`
  - Forward Hostname / IP: `<pi-lan-ip>`
  - Forward Port: `8090` (page + API now come from the gateway alone)
  - Cache Assets: off
  - Block Common Exploits: on
  - Websockets Support: off (not needed — MJPEG over plain HTTP)

- Custom Locations tab: **remove** the old `/dev` location (obsolete —
  `/` and `/dev/*` both come from :8090 now).

- SSL tab: as per your LAN policy (page works over plain http).

- Access tab:
  - Access List: the list enforcing auth (create under Access Lists;
    do not leave as Publicly Accessible).

Paths end up fused same-origin, e.g.:

- `http://<cam-domain>/` (static page from :8000)
- `http://<cam-domain>/dev/list` (gateway via custom location)
- `http://<cam-domain>/dev/ap-camera/live`
- `http://<cam-domain>/dev/ap-camera/snapshot`

## Notes

- Direct fallback (no NPM): `http://<pi-lan-ip>:8090/dev/ap-camera/live`
- Gateway command (reference): `PYTHONPATH=src /tmp/v720fp/bin/python scripts/ap_gateway.py --port 8090`
  (defaults: `--camera 192.168.169.1:6123 --listen 0.0.0.0`; see `docs/gateway-8090.md`)
- Port 8090 needs no CAP_NET_BIND and no interactive root to bind.
- Camera reboots: gateway retry handling is owned by #13.

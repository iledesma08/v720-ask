# NPM + AdGuard recipe for camera web (validated 2026-09-07)

Goal: `http://camara.lan/` serves `static/index.html` and
`http://camara.lan/dev/*` hits the AP-directo gateway (`:8090`),
both behind NPM auth. Gateway owns camera reconnect retries (see #13);
this doc is only the frontal recipe.

## 0. DNS rewrite in AdGuard Home (port 3000)

Filters > DNS rewrites > Add: Domain `camara.lan`, Answer `192.168.0.204`.
The browsing PC must use AdGuard as its DNS (else add
`192.168.0.204 camara.lan` to the PC hosts file). Verify from the PC:
`nslookup camara.lan` → `192.168.0.204`.

## 1. Static server for the page (:8000)

```bash
cd /mnt/data/docker/opencode/projects/v720-ask
python3 -m http.server 8000 --directory static
```

(Manual for now; a second systemd unit is future work, not #12.)

## 2. Proxy Host recipe (NPM UI, port 81)

Hosts > Proxy Hosts > Add Proxy Host:

- Details tab:
  - Domain Names: `camara.lan`
  - Scheme: `http`
  - Forward Hostname / IP: `192.168.0.204`
  - Forward Port: `8000` (the static page; NOT 8090)
  - Cache Assets: off
  - Block Common Exploits: on
  - Websockets Support: off (not needed — MJPEG over plain HTTP)

- Custom Locations tab > Add Location:
  - Location: `/dev`
  - Scheme: `http`
  - Forward Hostname / IP: `192.168.0.204`
  - Forward Port: `8090` (the gateway)

- SSL tab: as per your LAN policy (page works over plain http).

- Access tab:
  - Access List: the list enforcing auth (create under Access Lists;
    do not leave as Publicly Accessible).

Paths end up fused same-origin, e.g.:

- `http://camara.lan/` (static page from :8000)
- `http://camara.lan/dev/list` (gateway via custom location)
- `http://camara.lan/dev/ap-camera/live`
- `http://camara.lan/dev/ap-camera/snapshot`

## Notes

- Direct fallback (no NPM): `http://192.168.0.204:8090/dev/ap-camera/live`
- Gateway command (reference): `PYTHONPATH=src /tmp/v720fp/bin/python scripts/ap_gateway.py --port 8090`
  (defaults: `--camera 192.168.169.1:6123 --listen 0.0.0.0`; see `docs/gateway-8090.md`)
- Port 8090 needs no CAP_NET_BIND and no interactive root to bind.
- Camera reboots: gateway retry handling is owned by #13.

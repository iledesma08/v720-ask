# 8090 passthrough gateway HOWTO (Pi 5, direct-AP)

Ports: pick a free LAN port for the gateway (**8090** here); keep clear
of NPM (:80/443), AdGuard (:53) and your own services (`ss -tlnp` to check).

1. Camera in AP mode (`Nax_*`), `wlan0` associated, `nc -vz 192.168.169.1 6123`
2. Start: `PYTHONPATH=src /tmp/v720fp/bin/python scripts/ap_gateway.py --port 8090`
3. `curl -s http://127.0.0.1:8090/dev/list` → `[{"uid":"ap-camera",...}]`
4. Browser (PC on LAN): `http://<pi-lan-ip>:8090/dev/ap-camera/live`
5. Snapshot: `curl -o s.jpg http://127.0.0.1:8090/dev/ap-camera/snapshot && file s.jpg`
6. One viewer at a time: second `/live` returns 503 `camera busy` (multi-viewer fan-out is tracked in #9)
7. Each session closes video (`code 0`) when the client disconnects: does not poison the camera (see #10)
8. If `502 no frame in time`: physical camera reset + retry once

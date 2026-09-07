# Gateway passthrough 8090 HOWTO (Pi 5, AP-directo)

Puertos ocupados en esta Pi: 80/443 npm, 53 adguard, 8080 bentopdf,
8081 metube, 8082 it-tools, 8083 stirling → el gateway usa **8090**.

1. Cámara en AP (`Nax_*`), `wlan0` asociado, `nc -vz 192.168.169.1 6123`
2. Levantar: `PYTHONPATH=src /tmp/v720fp/bin/python scripts/ap_gateway.py --port 8090`
3. `curl -s http://127.0.0.1:8090/dev/list` → `[{"uid":"ap-camera",...}]`
4. Navegador (PC en LAN): `http://192.168.0.204:8090/dev/ap-camera/live`
5. Snapshot: `curl -o s.jpg http://127.0.0.1:8090/dev/ap-camera/snapshot && file s.jpg`
6. Un viewer a la vez: segundo `/live` responde 503 `camera busy` (fan-out multi-viewer va en #9)
7. Cada sesión cierra video (`code 0`) al soltar el cliente: no re-envenena la cámara (ver #10)
8. Si `502 no frame in time`: reset físico de cámara + reintentar una vez

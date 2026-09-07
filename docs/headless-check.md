# Headless check HOWTO (Pi 5, AP-directo)

1. `iw dev wlan0 link` → SSID `Nax_*`, luego `ip -4 addr show wlan0`
2. `ping -c3 192.168.169.1` y `nc -vz 192.168.169.1 6123`
3. `python3 -m venv /tmp/v720fp && /tmp/v720fp/bin/pip install -r requirements-min.txt`
4. `PYTHONPATH=src /tmp/v720fp/bin/python scripts/v720_check.py --frames 3 --out /tmp`
5. `file /tmp/frame*.jpg` → JPEG 640x480 válidos = OK
6. El script reintenta init 3x solo; si agota (`BASEINFO_FAIL`): desenchufar cámara 5s, esperar 30s, reintentar una vez
7. No usar `nmap -p 1-10000` (cuelga el stack mínimo); solo `6123` importa
8. No instalar `requirements.txt` completo (opencv pesado, sin uso en gateway)
9. No correr dos lives seguidos sin esperar (estado `CMD 6 PCM`, ver #7)
10. Pegar `BASEINFO version` + `OK N frames` en el issue #10

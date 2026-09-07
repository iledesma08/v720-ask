# Headless check HOWTO (Pi 5, direct-AP)

1. `iw dev wlan0 link` → SSID `Nax_*`, then `ip -4 addr show wlan0`
2. `ping -c3 192.168.169.1` and `nc -vz 192.168.169.1 6123`
3. `python3 -m venv /tmp/v720fp && /tmp/v720fp/bin/pip install -r requirements-min.txt`
4. `PYTHONPATH=src /tmp/v720fp/bin/python scripts/v720_check.py --frames 3 --out /tmp`
5. `file /tmp/frame*.jpg` → valid 640x480 JPEGs = OK
6. The script retries init 3x on its own; if exhausted (`BASEINFO_FAIL`): unplug the camera for 5s, wait 30s, retry once
7. Do not use `nmap -p 1-10000` (hangs the minimal stack); only `6123` matters
8. Do not install the full `requirements.txt` (heavy opencv, unused in the gateway)
9. Do not run two lives back-to-back without waiting (`CMD 6 PCM` state, see #7)
10. Paste `BASEINFO version` + `OK N frames` into issue #10

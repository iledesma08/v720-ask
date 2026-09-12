# STA experiment runbook (#8)

Goal: prove the PTZ (FW `202509111709`) joins home WiFi and talks to a
fake-server. AP-direct stays production; this is a time-boxed daytime
experiment with physical-reset rollback.

## 0. Preconditions (do not skip)

- [ ] Daytime window (night alerts must work tonight).
- [ ] TP-Link 2.4GHz SSID + password at hand. The password is typed live,
      never stored (not in repo, issues, logs, or shell history files —
      prefer the env form below over `--password`).
- [ ] Reset procedure fresh in mind (`docs/camara-web.md` §9): one press,
      camera rejoins its own AP.
- [ ] Branch `feat/8-sta-experiment` checked out (this file + `sta_join.py`).

## 1. Fake-server up

```bash
# AdGuard Home (port 3000): DNS rewrites, add:
#   *.naxclow.com -> <Pi LAN IP, e.g. 192.168.0.204>
# Mosquitto (ephemeral, anonymous, experiment-only):
sudo docker run -d --rm --name sta-mosquitto -p 1883:1883 eclipse-mosquitto:2
# Capture (needs tshark or tcpdump on the Pi):
sudo tshark -i any -f "port 80 or port 1883 or port 6123 or port 29940" \
  -w /tmp/sta-$(date +%H%M).pcap &
echo $!  # keep the PID to stop it later
```

## 2. Stop the gateway (single-session camera)

```bash
cd /mnt/data/docker/opencode/projects/v720-ask
sudo docker compose -f docker-compose.yml stop
```

## 3. Dry run, then join

```bash
STA_WIFI_PASS='...' PYTHONPATH=src /tmp/v720fp/bin/python \
  scripts/sta_join.py --ssid 'TP-Link-ASK-2.4' --dry-run
STA_WIFI_PASS='...' PYTHONPATH=src /tmp/v720fp/bin/python \
  scripts/sta_join.py --ssid 'TP-Link-ASK-2.4'
```

Expected: `JOIN-SEND: answered {...}` (or TIMEOUT — the AP drops either
way by design). From here the camera is gone from `192.168.169.1`.

## 4. Verify (still without gateway)

```bash
# a) DHCP lease on the Archer for the camera (new device/hostname)
# b) In the pcap: POST getA9ConfCheck (or getDevInfo first on newer FW),
#    MQTT connect to the Pi broker
# c) ping the leased IP
```

Verdict rule: lease + cloud POST captured = STA viable. Anything less
(incl. silent camera) = not viable, roll back.

## 5. Roll back (always, even on success — migration is a later decision)

1. Stop the capture (`kill <PID>` above).
2. Physical reset button once → camera reboots to its AP.
3. `iw dev wlan0 link` shows `Nax_*` again; `ping -c3 192.168.169.1`.
4. `sudo docker compose -f docker-compose.yml up -d`; check
   `curl -s http://127.0.0.1:8090/dev/list` and live.
5. Stop + remove mosquitto (`sudo docker stop sta-mosquitto`);
   keep or revert the AdGuard rewrite (harmless either way, note choice).
6. Commit pcap notes (NOT the pcap itself if huge — summarize) + verdict
   to #8.

## Results 2026-09-11 (experiment ran, rolled back to AP)

- **JOIN (204) accepted**: AP `Nax_*` dropped; DHCP lease `rtthread`
  `192.168.0.22` on 2.4G (MAC matches camera). Join path works on this FW.
- **DNS hijack works**: camera queried `v720.p2p.naxclow.com` +
  `v720.naxclow.com`, AdGuard rewrote both (Query Log proof).
- **Cloud contact**: `POST /app/api/ApiSysDevices/getDevInfo`
  (`devicesCode=5c00200204AB` stable, fresh `random`/`token` per boot).
  Retried ×2–3 with identical params, then silent. No `getA9ConfCheck`,
  no MQTT — even answering the documented payload
  (`code 200 + userInfo_state/userId/ota_url`) with
  `Content-Type: application/json`.
- **Stale config found**: status answer carried
  `wifiName: 'Mi Internetcc-2.4Ghz'` (prior STA config, not ours).
- **NOT tried**: real-cloud capture (declined — OTA update risk).
- **Infra lessons**: Pi :80 belongs to npm → listener on 18080 +
  iptables REDIRECT scoped to the camera IP only; mosquitto 2.x defaults
  to loopback (needs `listener + allow_anonymous` conf); no tshark on
  the Pi — tcpdump; start captures BEFORE the join and verify the file
  grows (first pcap never ran).
- **Rollback**: physical reset → AP back → gateway up, verified.
  AdGuard rewrite left in place for the next attempt.
- **Pin to AP (post-experiment learning)**: reset restores the AP but the
  STA credentials persist in flash and the camera re-roams on its own.
  Deny-list the camera MAC (`5C:F0:20:05:09:2C`) in the Archer
  (Clients → rtthread → Deny) — reversible from the same UI. Do NOT
  overwrite STA config with junk SSIDs over 204 (unpredictable on
  this FW).

## Failure modes seen before

- No answer to join (TIMEOUT): pre-existing parked behavior class (209/412
  also time out on this FW). Check DHCP anyway — the camera sometimes
  roams without answering.
- Wedged control (stale CMD 6 PCM to everything): stop hammering, wait,
  fresh session; gateway drain-resync covers its own sessions.
- Beep-loop after reboot: the known quirk, reset covers it.

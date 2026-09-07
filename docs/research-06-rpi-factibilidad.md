# Research #6 — Raspberry Pi 5 feasibility: camera + gateway

> Part of #1 · Read-only, no code was modified.
> Question: Can a single Raspberry Pi 5 run camera + gateway without degrading?
> Local sources: `requirements.txt`, `src/v720_sta.py`, `src/v720_http.py`, `src/a9_live.py`, `readme.md` (Issue #51), `fake_server.md`, `docs/uart.log`, `docs/fake-server-cap.pcapng`.
> Date: 2026-09-07.

## TL;DR — verdict

- **Direct-AP (`wlan0 → AP Nax_* 192.168.169.1:6123`) + MJPEG passthrough gateway: YES, feasible on Pi 5 even 1–2 GB.** 1 camera 640x480 MJPG 10fps ≈ 1.2–1.5 Mbps/viewer, <5% CPU, <100 MB real RAM if queues are bounded. 1–4 cameras/viewers fit comfortably.
- **With decode/overlay (`a9_live.py`: PIL→numpy→`cv2.putText`→`VideoWriter`): YES but on Pi 5 4/8 GB with cooling.** +~10–20% of 1 core at 10fps + ~1 MB/raw frame.
- **STA+fake-server on the same `wlan0`: NO with a single radio if the uplink is on another channel/band.** The robust design is `eth0`=uplink + `wlan0`=camera network, or a second USB adapter. See §2.
- **Real risk #1 of OOM:** `v720_http.py:90,132 Queue(16384)` is not "15 MB" but 16384 *items* → 245–800 MB per slow viewer; plus `v720_sta.py:115 _vframe = Queue()` unbounded. Bound to 30–60 frames.
- **Operational risks #2–3:** `NetworkManager` vs `dnsmasq:53` / `systemd-resolved` conflict, and port 80 without root. Both have standard mitigation (see table R-04, R-05).
- **`ThreadingHTTPServer` + `/dev/*` without auth: OK only on trusted LAN / behind proxy.** Do not expose directly; see R-06, R-07.
- **FW breakage `>=20241120173`: partially mitigated already.** The `getDevInfo` handler (`v720_http.py:239-241`) unblocks the case reported in `intx82/a9-v710#51`; no evidence of TLS/pinning. Fingerprinting for a different camera remains in #3.
- **Proposed decoupling:** `Transport` / `CameraDriver` / `MediaSink` (see §5) isolates `netifaces/numpy/cv2/PIL` behind 2 injectable functions.

## 1. CPU / MEM / network per camera

Measured base in code (read-only):

- `fake_server.md §9`: JPEG ~15 KB, fragmented into ~1000 B (`MSG_FLAG 250/251/252`), `RETRANSMISSION_CONFIRM (605)` every 100 ms for 10 fps. `v720_sta.py:410-434` implements the timer with `threading.Timer(0.1, …)`.
- `v720_http.py:90,132`: `q = Queue(16384)  # 15kb * 1024 ~ 15mb per camera` — erroneous comment (see below).
- `v720_http.py:108-113`: `multipart/x-mixed-replace; boundary="jpgboundary"`, one thread per viewer (`ThreadingHTTPServer`).
- `a9_live.py:29-48`: `numpy.array(Image.open(BytesIO(frame)))` + `cv2.putText ×2` + `VideoWriter(MJPG, 10, (640,480))` + `Timer(0.1, _save_video)`.

Estimate (current passthrough, no decode):

| Magnitude | Calculation | Result |
|---|---|---|
| Net video | 15 KB/frame × 10 fps | ~150 KB/s ≈ **1.2 Mbps** + multipart overhead (~60–120 B/frame, <1%) + TCP/IP 2–3% → **~1.25 Mbps/viewer** |
| If JPEG 30–60 KB (high quality) | 30–60 KB × 10 | 2.4–4.8 Mbps/viewer, still comfortable on Pi 5 GbE |
| 4 viewers | 4 × 1.25 | ~5 Mbps, <1% GbE |
| Passthrough CPU | `q.get + wfile.write`, I/O-bound, releases GIL | **<2–3%** estimated on Cortex-A76 |
| UDP frags | 15 KB / ~1400 MTU ≈ 11 frags; `append + put` per frag, 1 callback/frame (10 cb/s) | ~110 `put`/s, negligible |
| ACKs | 10 pkt/s × tens–hundreds of B | <10 KB/s |
| Raw MEM with decode only | 640×480×3 B | **0.92 MB/frame** + PIL/JPEG copy |

Primary sources: Pi 5 SoC BCM2712 4×A76@2.4 GHz, LPDDR4X, GbE — [raspberrypi.com/products/raspberry-pi-5](https://www.raspberrypi.com/products/raspberry-pi-5/) and [product brief RP-008348-DS](https://pip.raspberrypi.com/documents/RP-008348-DS-raspberry-pi-5-product-brief.pdf); `ThreadingHTTPServer` thread-per-request — [docs.python.org/3/library/http.server.html](https://docs.python.org/3/library/http.server.html) and [socketserver](https://docs.python.org/3/library/socketserver.html); `Queue.maxsize` = item count — [docs.python.org/3/library/queue.html](https://docs.python.org/3/library/queue.html); `VideoWriter MJPG` — [docs.opencv.org/4.8.0](https://docs.opencv.org/4.8.0/dd/d9e/classcv_1_1VideoWriter.html); `Image.open` lazy + `BytesIO` — [pillow.readthedocs.io](https://pillow.readthedocs.io/en/stable/reference/Image.html); GIL I/O vs CPU — [docs.python.org/3/library/threading.html](https://docs.python.org/3/library/threading.html) and [free-threading HOWTO](https://docs.python.org/3/howto/free-threading-python.html); `Timer` respawns thread — [threading.Timer](https://docs.python.org/3/library/threading.html); thermal/power 5V/5A — [thermal whitepaper RP-010139-WP-1](https://pip-assets.raspberrypi.com/categories/685-app-notes-guides-whitepapers/documents/RP-010139-WP-1-Use-case-specific%20thermal%20performance%20of%20Raspberry%20Pi%20SBCs.pdf) and [power-supplies.adoc](https://github.com/raspberrypi/documentation/blob/9de619f8/documentation/asciidoc/computers/raspberry-pi/power-supplies.adoc).

Conclusion: with Pi 5 4 GB + fan + official 27 W PSU, 1–4 passthrough cameras fit comfortably; with overlay/decode budget +~1 core.

## 2. Single 2.4 GHz radio — topology

- Pi 5 is **dual-band but single radio** (`Dual-band 802.11ac`, CYW43455-family chip). They are not two radios. Source: [raspberrypi.com/products/raspberry-pi-5](https://www.raspberrypi.com/products/raspberry-pi-5/).
- AP+STA concurrency on `wlan0` requires the **same channel** (`#{managed}<=1,#{AP}<=1,#channels<=1` in `iw list`); the virtual AP hangs off the STA channel, mini-AP ~8 clients, unstable. Source: [docs.raspap.com AP+STA](https://docs.raspap.com/features-experimental/ap-sta/) + official AP base [raspberrypi.com/documentation/...#host-a-wireless-network](https://www.raspberrypi.com/documentation/computers/configuration.html#host-a-wireless-network-from-your-raspberry-pi).
- Issue question (`wlan0` STA to `TP-Link-ASK` + talking to AP `Nax_* 192.168.169.1` at once): **only viable experimentally on the same channel, or with a second USB Wi-Fi.** If the uplink is on 5 GHz and the camera (BL7252, 2.4 GHz only) is on 2.4 GHz, it is impossible with one radio. Wi-Fi is half-duplex and AP+STA re-transmits everything on the same spectrum — fragmented MJPEG + 100 ms ACK is jitter-sensitive.
- Robust design already verified by the user (`eth0 192.168.0.204/24` + `wlan0 192.168.169.x/24`, ping + 6123 open, -45 dBm): **`eth0`=uplink, `wlan0`=STA to `Nax_*`**. Do not make `wlan0` do both. STA to `TP-Link-ASK` unconfirmed on DHCP — left for #3/#4.
- `bind('', 6123)` listens on both interfaces (fine), but `v720_http.py:248-259 getA9ConfCheck.host = get_ip(default_gw,80)` may return the uplink IP instead of the one seen by the camera when dual-homed. Verify with `ss -tulpn` + `ip route get <cam-IP>`.

## 3. Risk table + mitigations

| # | Risk | Local evidence | Impact | Mitigation (standard) | Primary source |
|---|---|---|---|---|---|
| R-01 | **OOM from `Queue(16384)` + unbounded `_vframe`** (245–800 MB/slow viewer; infinite `_vframe` if HTTP is slow) | `v720_http.py:90,132`, `v720_sta.py:115,446-466` | High — kills gateway with 2 slow viewers | Bound to 30–60 frames (`maxsize=30`), drop-oldest policy (`get_nowait` if `full`), `snapshot()` with `Queue(1)` already exists as pattern | [queue.Queue maxsize](https://docs.python.org/3/library/queue.html) |
| R-02 | **`Timer(0.1)` churn**: 1 new thread every 100 ms/camera (10 threads/s) | `v720_sta.py:410-419` | Medium — GC/GIL, time drift | Single loop `Event.wait(0.1)` + `send` in same UDP thread | [threading.Timer](https://docs.python.org/3/library/threading.html) |
| R-03 | **Single-radio contention** if `wlan0` does STA+AP/camera at once | §2 | High — jitter, loss, retrans storm | `eth0` uplink + `wlan0` camera-only; or 2nd USB Wi-Fi; pin 2.4 GHz channel; STA to `TP-Link-ASK` only if same channel (see #4) | [Pi 5 spec](https://www.raspberrypi.com/products/raspberry-pi-5/), [RaspAP AP+STA](https://docs.raspap.com/features-experimental/ap-sta/) |
| R-04 | **`NetworkManager(shared)→dnsmasq` vs `dnsmasq.service` vs `systemd-resolved:53` conflict** (`address already in use :53`) | `readme.md` DNS Redirection, `fake_server.md` NM 10.42.0.1 | High — DNS hijack `*.naxclow.com` does not come up | Do not launch 2 dnsmasqs; NM-shared snippets in `/etc/NetworkManager/dnsmasq-shared.d/`; `dns=dnsmasq` plugin uses `dnsmasq.d/`; `resolved`: `DNSStubListener=no` + re-point `resolv.conf` | [NM ipv4 shared](https://www.networkmanager.dev/docs/api/latest/settings-ipv4.html), [NetworkManager.conf](https://networkmanager.dev/docs/api/latest/NetworkManager.conf.html), [resolved.conf](https://www.freedesktop.org/software/systemd/man/latest/resolved.conf.html), [systemd-resolved.service](https://www.freedesktop.org/software/systemd/man/systemd-resolved.service.html), [dnsmasq-man address=/server=/](https://dnsmasq.org/docs/dnsmasq-man.html) |
| R-05 | **Port 80 without root → `PermissionError`** | `v720_http.py:49-52,291-295` | Medium — gateway does not start without sudo | Order: `systemd AmbientCapabilities=CAP_NET_BIND_SERVICE` > setcap binary > `authbind` > `iptables REDIRECT`; or `--proxy-port` + nginx/caddy in front; `sysctl ip_unprivileged_port_start=80` works but opens 80–1023 and is volatile (persist in `/etc/sysctl.d/`) | [capabilities(7)](https://man7.org/linux/man-pages/man7/capabilities.7.html), [ip-sysctl](https://docs.kernel.org/networking/ip-sysctl.html), [systemd.exec](https://www.freedesktop.org/software/systemd/man/systemd.exec.html) |
| R-06 | **`ThreadingHTTPServer` thread-per-request without limit + `http.server` non-prod** → DoS from slow viewers/bursts | `v720_http.py:41,284` | Medium-High on LAN | Loopback/dev only; in prod reverse proxy (nginx/caddy `limit_conn/rate/timeout`) or bounded pool (`ThreadPoolExecutor` + `verify_request`/allowlist) | [http.server](https://docs.python.org/3/library/http.server.html), [socketserver](https://docs.python.org/3/library/socketserver.html) |
| R-07 | **`/dev/*` without auth + `static/` listing/symlink + plain-text MJPEG** → any LAN host enumerates (`/dev/list`) and watches video/snaps | `v720_http.py:72-86,203-220` do_GET, `STATIC_DIR`, `do_POST` fake 200 | High (privacy) even on LAN | Auth token/session, non-enumerable IDs, uniform 403, rate-limit; `list_directory`→404, no symlinks, `CSP/nosniff` headers; HTTPS only (LAN/Tailscale proxy cert), `Cache-Control: no-store` | [OWASP IDOR](https://owasp.org/www-community/attacks/insecure_direct_object_reference), [OWASP A01](https://owasp.org/Top10/A01_2021-Broken_Access_Control/), [OWASP A02 crypto](https://owasp.org/Top10/A02_2021-Cryptographic_Failures/), [HTTPSServer](https://docs.python.org/3/library/http.server.html) |
| R-08 | **Harsh shutdown / leaks: deprecated `setDaemon()` + `_lstnr_cnt` without Lock + `Queue` without `task_done`** | `v720_sta.py:34,38,163,169,372-399`, `v720_http.py` handlers | Medium — resources (VideoWriter/sockets) left unclosed | `daemon=True` + stop `Event`, `Lock`/atomic `Counter`, `maxsize` + `put_nowait/discard`, ordered `join()+shutdown()`, `server_close()` | [threading daemons](https://docs.python.org/3/library/threading.html), [socketserver server_close](https://docs.python.org/3/library/socketserver.html) |
| R-09 | **systemd/Docker without sandbox** (root, writable FS, `ports` on 0.0.0.0) | future deployment | Medium | systemd: `User=v720`, `NoNewPrivileges`, `ProtectSystem=strict+ReadWritePaths=/var/lib/v720`, `PrivateTmp/Devices`, `MemoryMax/TasksMax`, `Restart=on-failure`. Docker: `-p 127.0.0.1:8080:80`, `USER`, `--read-only+--tmpfs`, `--cap-drop ALL`, `no-new-privileges`, `--memory/--cpus` | [systemd.exec](https://man.archlinux.org/man/systemd.exec.5.en), [systemd.service](https://man.archlinux.org/man/systemd.service.5.en), [docker port publishing](https://docs.docker.com/engine/network/port-publishing/), [docker publishing-ports](https://docs.docker.com/get-started/docker-concepts/running-containers/publishing-ports/), [docker tmpfs](https://docs.docker.com/engine/storage/tmpfs/), [docker run](https://docs.docker.com/reference/cli/docker/container/run/) |
| R-10 | **SD wear** (journald + `print/log_message` + VideoWriter/snaps) → exhausted P/E, corrupt ext4/vfat | `a9_live.py:46-61 VideoWriter`, `log.py`, `v720_http` logs | Medium (24/7) | `journald Storage=volatile+SystemMaxUse=32M`, app logs to tmpfs (`/run/v720`), video to data partition/SSD-NVMe on Pi 5, swap off/zram; overlayfs only if appliance with state on dedicated RW | [raspberrypi configuration](https://www.raspberrypi.com/documentation/computers/configuration.html), [resilient FS whitepaper RP-003610-WP](https://pip.raspberrypi.com/categories/685-whitepapers-app-notes/documents/RP-003610-WP/Making-a-more-resilient-file-system.pdf), [filesystem overlay](https://www.raspberrypi.com/documentation/configuration/filesystem.md), [kernel overlayfs](https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html) |
| R-11 | **Broken fake-server on FW `>=20241120173`** if the different camera ships new FW (new `getDevInfo` before `getA9ConfCheck`; more `.cn` domains) | `readme.md:235-247`, `v720_http.py:239-241`, `fake_server.md` | Medium-High for STA | Already patched `getDevInfo→{userInfo_state:1}` for case #51; prior fingerprint in #3 (see §4); `dnsmasq address=/naxclow.com/<IP>` covers apex+subs; `mosquitto listener 1883 <LAN-IP>+allow_anonymous` | [intx82/a9-v720#51](https://github.com/intx82/a9-v720/issues/51), [mosquitto-8](https://mosquitto.org/man/mosquitto-8.html), [mosquitto-conf-5](https://mosquitto.org/man/mosquitto-conf-5.html) |
| R-12 | **Old pinned deps** (`netifaces` archived, `numpy1` vs `opencv` ABI, `opencv-full` requires libGL, `Pillow9` without CVEs) | `requirements.txt` | Medium — does not build on bookworm/aarch64 | `netifaces→netifaces-plus/ifaddr` behind `get_lan_ip()`; `numpy>=1.26,<3`; `opencv-python-headless` + lazy import; `Pillow>=10,<13` behind `decode_jpeg()`; `tqdm` no-op fallback; `xmltodict→ET` stdlib | [netifaces 0.11.0](https://pypi.org/project/netifaces/0.11.0/) ([archived](https://github.com/al45tair/netifaces)), [netifaces-plus](https://pypi.org/project/netifaces-plus/), [ifaddr](https://pypi.org/project/ifaddr/), [numpy 1.24.4](https://pypi.org/project/numpy/1.24.4/) ([1.26 notes](https://numpy.org/doc/2.0/release/1.26.0-notes.html), [2.0 notes](https://numpy.org/doc/2.3/release/2.0.0-notes.html)), [opencv 4.8.0.76](https://pypi.org/project/opencv-python/4.8.0.76/) ([pip install](https://docs.opencv.org/5.0/py_tutorials/py_setup/py_pip_install/py_pip_install.html)), [Pillow 9.5.0](https://pypi.org/project/pillow/9.5.0/) vs [11.1.0](https://pypi.org/project/pillow/11.1.0/) ([releasenotes](https://pillow.readthedocs.io/en/stable/releasenotes/)) |

Notes R-04/R-05/R-11: `address=/naxclow.com/IP` answers IP for apex and subs; `server=/naxclow.com/IP` only directs to that upstream (redundant but harmless to set both). `mosquitto ≥2.0` is loopback-only without config. `ipv4.method=shared` gives `10.42.x.0/24` (not `192.168.169/24`): do not mix NM-shared topology with camera-AP.

## 4. FW `>=20241120173` and domains — status

- Sole primary source of the change: [intx82/a9-v720#51](https://github.com/intx82/a9-v720/issues/51). On FW `202503081631/202411201737` the camera requests **`POST /app/api/ApiSysDevices/getDevInfo?devicesCode=…` before `getA9ConfCheck`**; if the fake returns 404 it never shows up in `/dev/list`. Quoted real response: `{"code":200,…,"data":{"userInfo_userId":" ","ota_url":null,…,"userInfo_state":1,…}}`. The patch `{"code":200,"message":"OK","data":{"userInfo_state":1}}` **is already applied** in this repo (`v720_http.py:239-241`).
- Domains observed by `@0x3dlux` (same issue, no official Naxclow doc): `dl2.naxclow.com, dl2.naxclow.com.cn, home.naxclow.com, logo.naxclow.com.cn, v720.naxclow.com, v720.p2p.naxclow.com`.
- Two local bugs reported in the issue (not FW bugs): `get_ip()` returns WAN if the server is the router (`v720_http.py:258`), and `json.dumps(ret)` without padding truncates ~2 B (`v720_http.py:266`).
- **No evidence** of TLS/cert-pinning, MQTT with auth, or suppression under DNS-hijack in `pcap/uart`: captures show plain HTTP :80 and MQTT :1883. Beken only publishes chip [BK7252](https://www.bekencorp.com/en/goods/detail/cid/22.html), SDK [bdk_rtt](https://github.com/YangAlex66/bdk_rtt) and variant [beken7252-opencam](https://github.com/daniel-dona/beken7252-opencam). Board `IOT_V1.2D250609` with AP `ZIOTA_*` ≠ Naxclow (not supported).
- Fingerprint for #3 (different camera, HITL from the Pi): `tshark -i any -f "port 80 or 1883 or 6123 or 29940" -w /tmp/v720.pcap` + `dnsmasq --log-queries` (does it ask `getDevInfo`? `*.cn`? `home/logo/dl2` OTA?) + `python3 src/a9_naxclow.py -sv` (404 on `getDevInfo` = new FW without handler) + `mosquitto_sub -t '#' -h v720.p2p.naxclow.com -v` (`Info` with `"version"`) + `version` from `baseinfo 4` (`v720_sta.py:352`) to tell `202212011602` apart from new.

## 5. Decoupling points — `CameraDriver` / `Transport` / `MediaSink`

Proposed cut (design only, no code touched in wayfinding):

```python
class Transport(Protocol):  # cuts netcl_tcp/udp + netsrv_tcp/udp
    def open(self) -> None: ...
    def close(self) -> None: ...
    def send(self, b: bytes) -> None: ...
    def recv(self) -> bytes | None: ...

class CameraDriver(Protocol):  # cuts v720_ap / v720_sta
    id: str
    def cap_live(self, on_rcv: Callable[[int, bytes], None]) -> None: ...
    def cap_stop(self) -> None: ...
    def snapshot(self, timeout: float = 5.0) -> bytes: ...

class MediaSink(Protocol):  # cuts a9_live + v720_http handlers
    def on_video(self, jpeg: bytes) -> None: ...
    def on_audio(self, g711: bytes) -> None: ...
```

- `v720_http` depends only on `CameraDriver` (plus `get_frame/snapshot` without `Queue` in http).
- `a9_live` only on `MediaSink + CameraDriver`.
- `netifaces` (only `v720_http.py:14,248 gateways()`) → injectable `get_lan_ip(iface)` (`netifaces-plus`/`ifaddr`/stdlib).
- `numpy+cv2+PIL` (only `a9_live.py`) → injectable `decode_jpeg()` + `VideoSink`, lazy `import cv2`, `opencv-headless`.
- `tqdm` (download only) → no-op fallback; `xmltodict` (only `prot_xml_udp`) → `xml.etree.ElementTree`.
- Real usage verified by grep: `netifaces→v720_http`, `numpy/cv2/PIL→a9_live`, `tqdm→a9_naxclow`, `xmltodict→prot_xml_udp`.

This leaves decision #5 (minimal adaptation vs modernized gateway Docker/web/deps) as a *wiring* change, not a protocol one.

## 6. Answer to the question + what's missing for #4/#5

**Yes, one Pi 5 handles camera + gateway in direct-AP passthrough; STA+fake-server requires a dedicated network (eth0 uplink) and fingerprinting of the different camera (#3) before choosing topology (#4) and scope (#5).** Wayfinder order: #3 (HITL fingerprint) → #4 (AP vs STA) → #5 (minimal vs modernized). This ticket (#6, AFK) is answered with this note + table R-01…R-12.

## Sources (primary)

Pi 5 spec and product brief; thermal whitepaper; power-supplies; `http.server`/`socketserver`/`queue`/`threading`; OpenCV VideoWriter; Pillow Image; free-threading; kernel ip-sysctl; capabilities(7); systemd.exec/service; docker port-publishing/tmpfs/run; raspberrypi configuration + resilient-FS whitepaper + overlayfs (kernel + raspi docs); NetworkManager ipv4 + NetworkManager.conf; resolved.conf/service; dnsmasq-man; mosquitto-8/conf-5; RaspAP AP+STA; PyPI netifaces/numpy/opencv/Pillow/tqdm/xmltodict + numpy release notes + opencv pip guide + Pillow releasenotes; Beken BK7252 + bdk_rtt; `intx82/a9-v720#51`. URLs inline in §§1–5.

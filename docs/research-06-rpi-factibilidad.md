# Research #6 — Factibilidad Raspberry Pi 5: cámara + gateway

> Part of #1 · Solo lectura, no se modificó código.
> Pregunta: ¿Puede una sola Raspberry Pi 5 correr cámara + gateway sin degradarse?
> Fuentes locales: `requirements.txt`, `src/v720_sta.py`, `src/v720_http.py`, `src/a9_live.py`, `readme.md` (Issue #51), `fake_server.md`, `docs/uart.log`, `docs/fake-server-cap.pcapng`.
> Fecha: 2026-09-07.

## TL;DR — veredicto

- **AP-directo (`wlan0 → AP Nax_* 192.168.169.1:6123`) + gateway MJPEG passthrough: SÍ, factible en Pi 5 incluso 1–2 GB.** 1 cámara 640x480 MJPG 10fps ≈ 1.2–1.5 Mbps/viewer, <5% CPU, <100 MB RAM real si se acotan colas. 1–4 cámaras/viewers caben holgados.
- **Con decode/overlay (`a9_live.py`: PIL→numpy→`cv2.putText`→`VideoWriter`): SÍ pero en Pi 5 4/8 GB con cooling.** +~10–20% de 1 core a 10fps + ~1 MB/frame raw.
- **STA+fake-server en el mismo `wlan0`: NO con un solo radio si el uplink está en otro canal/banda.** El diseño robusto es `eth0`=uplink + `wlan0`=red cámara, o segundo adaptador USB. Ver §2.
- **Riesgo #1 real de OOM:** `v720_http.py:90,132 Queue(16384)` no son "15 MB" sino 16384 *items* → 245–800 MB por viewer lento; más `v720_sta.py:115 _vframe = Queue()` sin cota. Acotar a 30–60 frames.
- **Riesgos #2–3 operativos:** conflicto `NetworkManager` vs `dnsmasq:53` / `systemd-resolved`, y puerto 80 sin root. Ambos tienen mitigación estándar (ver tabla R-04, R-05).
- **`ThreadingHTTPServer` + `/dev/*` sin auth: OK solo en LAN confiable / tras proxy.** No exponer directo; ver R-06, R-07.
- **Rotura FW `>=20241120173`: parcialmente mitigada ya.** El handler `getDevInfo` (`v720_http.py:239-241`) desbloquea el caso reportado en `intx82/a9-v710#51`; no hay evidencia de TLS/pinning. Queda fingerprint para cámara distinta en #3.
- **Desacople propuesto:** `Transport` / `CameraDriver` / `MediaSink` (ver §5) aísla `netifaces/numpy/cv2/PIL` tras 2 funciones inyectables.

## 1. CPU / MEM / red por cámara

Base medida en código (solo lectura):

- `fake_server.md §9`: JPEG ~15 KB, fragmentado en ~1000 B (`MSG_FLAG 250/251/252`), `RETRANSMISSION_CONFIRM (605)` cada 100 ms para 10 fps. `v720_sta.py:410-434` implementa el timer con `threading.Timer(0.1, …)`.
- `v720_http.py:90,132`: `q = Queue(16384)  # 15kb * 1024 ~ 15mb per camera` — comentario erróneo (ver abajo).
- `v720_http.py:108-113`: `multipart/x-mixed-replace; boundary="jpgboundary"`, un thread por viewer (`ThreadingHTTPServer`).
- `a9_live.py:29-48`: `numpy.array(Image.open(BytesIO(frame)))` + `cv2.putText ×2` + `VideoWriter(MJPG, 10, (640,480))` + `Timer(0.1, _save_video)`.

Estimación (passthrough actual, sin decode):

| Magnitud | Cálculo | Resultado |
|---|---|---|
| Neto video | 15 KB/frame × 10 fps | ~150 KB/s ≈ **1.2 Mbps** + overhead multipart (~60–120 B/frame, <1%) + TCP/IP 2–3% → **~1.25 Mbps/viewer** |
| Si JPEG 30–60 KB (calidad alta) | 30–60 KB × 10 | 2.4–4.8 Mbps/viewer, aún holgado en GbE Pi 5 |
| 4 viewers | 4 × 1.25 | ~5 Mbps, <1% GbE |
| CPU passthrough | `q.get + wfile.write`, I/O-bound, libera GIL | **<2–3%** estimado en Cortex-A76 |
| UDP frags | 15 KB / ~1400 MTU ≈ 11 frags; `append + put` por frag, 1 callback/frame (10 cb/s) | ~110 `put`/s, despreciable |
| ACKs | 10 pkt/s × decenas–cientos B | <10 KB/s |
| MEM raw solo con decode | 640×480×3 B | **0.92 MB/frame** + copia PIL/JPEG |

Fuentes primarias: Pi 5 SoC BCM2712 4×A76@2.4 GHz, LPDDR4X, GbE — [raspberrypi.com/products/raspberry-pi-5](https://www.raspberrypi.com/products/raspberry-pi-5/) y [product brief RP-008348-DS](https://pip.raspberrypi.com/documents/RP-008348-DS-raspberry-pi-5-product-brief.pdf); `ThreadingHTTPServer` thread-por-request — [docs.python.org/3/library/http.server.html](https://docs.python.org/3/library/http.server.html) y [socketserver](https://docs.python.org/3/library/socketserver.html); `Queue.maxsize` = nº items — [docs.python.org/3/library/queue.html](https://docs.python.org/3/library/queue.html); `VideoWriter MJPG` — [docs.opencv.org/4.8.0](https://docs.opencv.org/4.8.0/dd/d9e/classcv_1_1VideoWriter.html); `Image.open` lazy + `BytesIO` — [pillow.readthedocs.io](https://pillow.readthedocs.io/en/stable/reference/Image.html); GIL I/O vs CPU — [docs.python.org/3/library/threading.html](https://docs.python.org/3/library/threading.html) y [free-threading HOWTO](https://docs.python.org/3/howto/free-threading-python.html); `Timer` respawnea thread — [threading.Timer](https://docs.python.org/3/library/threading.html); térmica/alimentación 5V/5A — [whitepaper térmico RP-010139-WP-1](https://pip-assets.raspberrypi.com/categories/685-app-notes-guides-whitepapers/documents/RP-010139-WP-1-Use-case-specific%20thermal%20performance%20of%20Raspberry%20Pi%20SBCs.pdf) y [power-supplies.adoc](https://github.com/raspberrypi/documentation/blob/9de619f8/documentation/asciidoc/computers/raspberry-pi/power-supplies.adoc).

Conclusión: con Pi 5 4 GB + fan + fuente oficial 27 W, 1–4 cámaras passthrough van sobradas; con overlay/decode contar +~1 core.

## 2. Un solo radio 2.4 GHz — topología

- Pi 5 es **dual-band pero un solo radio** (`Dual-band 802.11ac`, chip familia CYW43455). No son dos radios. Fuente: [raspberrypi.com/products/raspberry-pi-5](https://www.raspberrypi.com/products/raspberry-pi-5/).
- Concurrencia AP+STA en `wlan0` exige **mismo canal** (`#{managed}<=1,#{AP}<=1,#channels<=1` en `iw list`); el AP virtual cuelga del canal del STA, mini-AP ~8 clientes, inestable. Fuente: [docs.raspap.com AP+STA](https://docs.raspap.com/features-experimental/ap-sta/) + base AP oficial [raspberrypi.com/documentation/...#host-a-wireless-network](https://www.raspberrypi.com/documentation/computers/configuration.html#host-a-wireless-network-from-your-raspberry-pi).
- Pregunta del issue (`wlan0` STA a `TP-Link-ASK` + hablar a AP `Nax_* 192.168.169.1` a la vez): **solo viable experimental en mismo canal, o con segundo USB-WiFi.** Si el uplink está en 5 GHz y la cámara (BL7252, solo 2.4 GHz) en 2.4 GHz, imposible con un radio. Wi-Fi es half-duplex y AP+STA re-transmite todo en el mismo espectro — MJPEG fragmentado + ACK 100 ms es sensible a jitter.
- Diseño robusto ya verificado por el usuario (`eth0 192.168.0.204/24` + `wlan0 192.168.169.x/24`, ping + 6123 abierto, -45 dBm): **`eth0`=uplink, `wlan0`=STA a `Nax_*`**. No poner `wlan0` a hacer las dos cosas. STA a `TP-Link-ASK` no confirmado en DHCP — queda para #3/#4.
- `bind('', 6123)` escucha en ambas interfaces (bien), pero `v720_http.py:248-259 getA9ConfCheck.host = get_ip(default_gw,80)` puede devolver la IP del uplink en vez de la vista por la cámara con dual-homed. Verificar con `ss -tulpn` + `ip route get <IP-cam>`.

## 3. Tabla de riesgos + mitigaciones

| # | Riesgo | Evidencia local | Impacto | Mitigación (estándar) | Fuente primaria |
|---|---|---|---|---|---|
| R-01 | **OOM por `Queue(16384)` + `_vframe` sin cota** (245–800 MB/viewer lento; `_vframe` infinita si HTTP lento) | `v720_http.py:90,132`, `v720_sta.py:115,446-466` | Alto — mata gateway con 2 viewers lentos | Acotar a 30–60 frames (`maxsize=30`), política drop-oldest (`get_nowait` si `full`), `snapshot()` con `Queue(1)` ya existe como patrón | [queue.Queue maxsize](https://docs.python.org/3/library/queue.html) |
| R-02 | **Churn `Timer(0.1)`**: 1 thread nuevo cada 100 ms/cámara (10 hilos/s) | `v720_sta.py:410-419` | Medio — GC/GIL, deriva temporal | Un solo loop `Event.wait(0.1)` + `send` en mismo hilo UDP | [threading.Timer](https://docs.python.org/3/library/threading.html) |
| R-03 | **Contención un solo radio** si `wlan0` hace STA+AP/cámara a la vez | §2 | Alto — jitter, pérdida, retrans tormenta | `eth0` uplink + `wlan0` solo cámara; o 2º USB-WiFi; fijar canal 2.4 GHz; STA a `TP-Link-ASK` solo si mismo canal (ver #4) | [Pi 5 spec](https://www.raspberrypi.com/products/raspberry-pi-5/), [RaspAP AP+STA](https://docs.raspap.com/features-experimental/ap-sta/) |
| R-04 | **Conflicto `NetworkManager(shared)→dnsmasq` vs `dnsmasq.service` vs `systemd-resolved:53`** (`address already in use :53`) | `readme.md` DNS Redirection, `fake_server.md` NM 10.42.0.1 | Alto — DNS hijack `*.naxclow.com` no levanta | No lanzar 2 dnsmasq; snippets NM-shared en `/etc/NetworkManager/dnsmasq-shared.d/`; plugin `dns=dnsmasq` usa `dnsmasq.d/`; `resolved`: `DNSStubListener=no` + re-apuntar `resolv.conf` | [NM ipv4 shared](https://www.networkmanager.dev/docs/api/latest/settings-ipv4.html), [NetworkManager.conf](https://networkmanager.dev/docs/api/latest/NetworkManager.conf.html), [resolved.conf](https://www.freedesktop.org/software/systemd/man/latest/resolved.conf.html), [systemd-resolved.service](https://www.freedesktop.org/software/systemd/man/systemd-resolved.service.html), [dnsmasq-man address=/server=/](https://dnsmasq.org/docs/dnsmasq-man.html) |
| R-05 | **Puerto 80 sin root → `PermissionError`** | `v720_http.py:49-52,291-295` | Medio — gateway no arranca sin sudo | Orden: `systemd AmbientCapabilities=CAP_NET_BIND_SERVICE` > binario con setcap > `authbind` > `iptables REDIRECT`; o `--proxy-port` + nginx/caddy delante; `sysctl ip_unprivileged_port_start=80` funciona pero abre 80–1023 y es volátil (persistir en `/etc/sysctl.d/`) | [capabilities(7)](https://man7.org/linux/man-pages/man7/capabilities.7.html), [ip-sysctl](https://docs.kernel.org/networking/ip-sysctl.html), [systemd.exec](https://www.freedesktop.org/software/systemd/man/systemd.exec.html) |
| R-06 | **`ThreadingHTTPServer` thread-por-request sin límite + `http.server` no-prod** → DoS por viewers lentos/burst | `v720_http.py:41,284` | Medio-Alto en LAN | Solo loopback/dev; en prod reverse-proxy (nginx/caddy `limit_conn/rate/timeout`) o pool acotado (`ThreadPoolExecutor` + `verify_request`/allowlist) | [http.server](https://docs.python.org/3/library/http.server.html), [socketserver](https://docs.python.org/3/library/socketserver.html) |
| R-07 | **`/dev/*` sin auth + `static/` listing/symlink + MJPEG en claro** → cualquier host LAN enumera (`/dev/list`) y ve video/snaps | `v720_http.py:72-86,203-220` do_GET, `STATIC_DIR`, `do_POST` fake 200 | Alto (privacidad) aunque LAN | Auth token/session, IDs no enumerables, 403 uniforme, rate-limit; `list_directory`→404, sin symlinks, headers `CSP/nosniff`; solo HTTPS (proxy cert LAN/Tailscale), `Cache-Control: no-store` | [OWASP IDOR](https://owasp.org/www-community/attacks/insecure_direct_object_reference), [OWASP A01](https://owasp.org/Top10/A01_2021-Broken_Access_Control/), [OWASP A02 crypto](https://owasp.org/Top10/A02_2021-Cryptographic_Failures/), [HTTPSServer](https://docs.python.org/3/library/http.server.html) |
| R-08 | **Apagado brusco / leaks: `setDaemon()` deprecated + `_lstnr_cnt` sin Lock + `Queue` sin `task_done`** | `v720_sta.py:34,38,163,169,372-399`, `v720_http.py` handlers | Medio — recursos (VideoWriter/sockets) sin close | `daemon=True` + `Event` parada, `Lock`/`Counter` atómico, `maxsize` + `put_nowait/discard`, `join()+shutdown()` ordenado, `server_close()` | [threading daemons](https://docs.python.org/3/library/threading.html), [socketserver server_close](https://docs.python.org/3/library/socketserver.html) |
| R-09 | **systemd/Docker sin sandbox** (root, FS escribible, `ports` en 0.0.0.0) | despliegue futuro | Medio | systemd: `User=v720`, `NoNewPrivileges`, `ProtectSystem=strict+ReadWritePaths=/var/lib/v720`, `PrivateTmp/Devices`, `MemoryMax/TasksMax`, `Restart=on-failure`. Docker: `-p 127.0.0.1:8080:80`, `USER`, `--read-only+--tmpfs`, `--cap-drop ALL`, `no-new-privileges`, `--memory/--cpus` | [systemd.exec](https://man.archlinux.org/man/systemd.exec.5.en), [systemd.service](https://man.archlinux.org/man/systemd.service.5.en), [docker port publishing](https://docs.docker.com/engine/network/port-publishing/), [docker publishing-ports](https://docs.docker.com/get-started/docker-concepts/running-containers/publishing-ports/), [docker tmpfs](https://docs.docker.com/engine/storage/tmpfs/), [docker run](https://docs.docker.com/reference/cli/docker/container/run/) |
| R-10 | **SD wear** (journald + `print/log_message` + VideoWriter/snaps) → P/E agotados, corrupta ext4/vfat | `a9_live.py:46-61 VideoWriter`, `log.py`, `v720_http` logs | Medio (24/7) | `journald Storage=volatile+SystemMaxUse=32M`, logs app a tmpfs (`/run/v720`), vídeo a partición datos/SSD-NVMe Pi 5, swap off/zram; overlayfs solo si appliance con estado en RW dedicada | [raspberrypi configuration](https://www.raspberrypi.com/documentation/computers/configuration.html), [resilient FS whitepaper RP-003610-WP](https://pip.raspberrypi.com/categories/685-whitepapers-app-notes/documents/RP-003610-WP/Making-a-more-resilient-file-system.pdf), [filesystem overlay](https://www.raspberrypi.com/documentation/configuration/filesystem.md), [kernel overlayfs](https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html) |
| R-11 | **Fake-server roto en FW `>=20241120173`** si la cámara distinta trae FW nueva (nuevo `getDevInfo` antes de `getA9ConfCheck`; más dominios `.cn`) | `readme.md:235-247`, `v720_http.py:239-241`, `fake_server.md` | Medio-Alto para STA | Ya parcheado `getDevInfo→{userInfo_state:1}` para el caso #51; fingerprint previo en #3 (ver §4); `dnsmasq address=/naxclow.com/<IP>` cubre apex+subs; `mosquitto listener 1883 <IP-LAN>+allow_anonymous` | [intx82/a9-v720#51](https://github.com/intx82/a9-v720/issues/51), [mosquitto-8](https://mosquitto.org/man/mosquitto-8.html), [mosquitto-conf-5](https://mosquitto.org/man/mosquitto-conf-5.html) |
| R-12 | **Deps pinneadas viejas** (`netifaces` archivado, `numpy1` vs `opencv` ABI, `opencv-full` pide libGL, `Pillow9` sin CVEs) | `requirements.txt` | Medio — no compila en bookworm/aarch64 | `netifaces→netifaces-plus/ifaddr` tras `get_lan_ip()`; `numpy>=1.26,<3`; `opencv-python-headless` + import perezoso; `Pillow>=10,<13` tras `decode_jpeg()`; `tqdm` fallback no-op; `xmltodict→ET` stdlib | [netifaces 0.11.0](https://pypi.org/project/netifaces/0.11.0/) ([archivado](https://github.com/al45tair/netifaces)), [netifaces-plus](https://pypi.org/project/netifaces-plus/), [ifaddr](https://pypi.org/project/ifaddr/), [numpy 1.24.4](https://pypi.org/project/numpy/1.24.4/) ([1.26 notes](https://numpy.org/doc/2.0/release/1.26.0-notes.html), [2.0 notes](https://numpy.org/doc/2.3/release/2.0.0-notes.html)), [opencv 4.8.0.76](https://pypi.org/project/opencv-python/4.8.0.76/) ([pip install](https://docs.opencv.org/5.0/py_tutorials/py_setup/py_pip_install/py_pip_install.html)), [Pillow 9.5.0](https://pypi.org/project/pillow/9.5.0/) vs [11.1.0](https://pypi.org/project/pillow/11.1.0/) ([releasenotes](https://pillow.readthedocs.io/en/stable/releasenotes/)) |

Notas R-04/R-05/R-11: `address=/naxclow.com/IP` responde IP para apex y subs; `server=/naxclow.com/IP` solo dirige a ese upstream (redundante pero inofensivo poner ambas). `mosquitto ≥2.0` solo loopback sin config. `ipv4.method=shared` da `10.42.x.0/24` (no `192.168.169/24`): no mezclar topología NM-shared con AP-cámara.

## 4. FW `>=20241120173` y dominios — estado

- Única fuente primaria del cambio: [intx82/a9-v720#51](https://github.com/intx82/a9-v720/issues/51). En FW `202503081631/202411201737` la cámara pide **`POST /app/api/ApiSysDevices/getDevInfo?devicesCode=…` antes de `getA9ConfCheck`**; si el fake devuelve 404 nunca aparece en `/dev/list`. Respuesta real citada: `{"code":200,…,"data":{"userInfo_userId":" ","ota_url":null,…,"userInfo_state":1,…}}`. El parche `{"code":200,"message":"OK","data":{"userInfo_state":1}}` **ya está aplicado** en este repo (`v720_http.py:239-241`).
- Dominios observados por `@0x3dlux` (mismo issue, sin doc oficial Naxclow): `dl2.naxclow.com, dl2.naxclow.com.cn, home.naxclow.com, logo.naxclow.com.cn, v720.naxclow.com, v720.p2p.naxclow.com`.
- Dos bugs locales reportados en el issue (no de FW): `get_ip()` devuelve WAN si el server es el router (`v720_http.py:258`), y `json.dumps(ret)` sin padding trunca ~2 B (`v720_http.py:266`).
- **No hay evidencia** de TLS/cert-pinning, MQTT con auth, ni suppression ante DNS-hijack en `pcap/uart`: capturas muestran HTTP :80 y MQTT :1883 en claro. Beken solo publica chip [BK7252](https://www.bekencorp.com/en/goods/detail/cid/22.html), SDK [bdk_rtt](https://github.com/YangAlex66/bdk_rtt) y variante [beken7252-opencam](https://github.com/daniel-dona/beken7252-opencam). Placa `IOT_V1.2D250609` con AP `ZIOTA_*` ≠ Naxclow (no soportada).
- Fingerprint para #3 (cámara distinta, HITL desde la Pi): `tshark -i any -f "port 80 or 1883 or 6123 or 29940" -w /tmp/v720.pcap` + `dnsmasq --log-queries` (¿pide `getDevInfo`? ¿`*.cn`? ¿`home/logo/dl2` OTA?) + `python3 src/a9_naxclow.py -sv` (404 en `getDevInfo` = FW nueva sin handler) + `mosquitto_sub -t '#' -h v720.p2p.naxclow.com -v` (`Info` con `"version"`) + `version` de `baseinfo 4` (`v720_sta.py:352`) para distinguir `202212011602` vs nueva.

## 5. Puntos de desacople — `CameraDriver` / `Transport` / `MediaSink`

Corte propuesto (solo diseño, sin tocar código en wayfinding):

```python
class Transport(Protocol):  # corta netcl_tcp/udp + netsrv_tcp/udp
    def open(self) -> None: ...
    def close(self) -> None: ...
    def send(self, b: bytes) -> None: ...
    def recv(self) -> bytes | None: ...

class CameraDriver(Protocol):  # corta v720_ap / v720_sta
    id: str
    def cap_live(self, on_rcv: Callable[[int, bytes], None]) -> None: ...
    def cap_stop(self) -> None: ...
    def snapshot(self, timeout: float = 5.0) -> bytes: ...

class MediaSink(Protocol):  # corta a9_live + v720_http handlers
    def on_video(self, jpeg: bytes) -> None: ...
    def on_audio(self, g711: bytes) -> None: ...
```

- `v720_http` solo depende de `CameraDriver` (más `get_frame/snapshot` sin `Queue` en http).
- `a9_live` solo de `MediaSink + CameraDriver`.
- `netifaces` (solo `v720_http.py:14,248 gateways()`) → `get_lan_ip(iface)` inyectable (`netifaces-plus`/`ifaddr`/stdlib).
- `numpy+cv2+PIL` (solo `a9_live.py`) → `decode_jpeg()` + `VideoSink` inyectables, `import cv2` perezoso, `opencv-headless`.
- `tqdm` (solo download) → fallback no-op; `xmltodict` (solo `prot_xml_udp`) → `xml.etree.ElementTree`.
- Uso real verificado por grep: `netifaces→v720_http`, `numpy/cv2/PIL→a9_live`, `tqdm→a9_naxclow`, `xmltodict→prot_xml_udp`.

Esto deja la decisión #5 (mínima adaptación vs gateway modernizado Docker/web/deps) como cambio de *wiring*, no de protocolo.

## 6. Respuesta a la pregunta + qué falta para #4/#5

**Sí, una Pi 5 puede con cámara + gateway en AP-directo passthrough; STA+fake-server exige red dedicada (eth0 uplink) y fingerprint de la cámara distinta (#3) antes de elegir topología (#4) y alcance (#5).** Orden wayfinder: #3 (fingerprint HITL) → #4 (AP vs STA) → #5 (mínima vs modernizado). Este ticket (#6, AFK) queda respondido con esta nota + tabla R-01…R-12.

## Fuentes (primarias)

Pi 5 spec y product brief; whitepaper térmico; power-supplies; `http.server`/`socketserver`/`queue`/`threading`; OpenCV VideoWriter; Pillow Image; free-threading; kernel ip-sysctl; capabilities(7); systemd.exec/service; docker port-publishing/tmpfs/run; raspberrypi configuration + resilient-FS whitepaper + overlayfs (kernel + raspi docs); NetworkManager ipv4 + NetworkManager.conf; resolved.conf/service; dnsmasq-man; mosquitto-8/conf-5; RaspAP AP+STA; PyPI netifaces/numpy/opencv/Pillow/tqdm/xmltodict + numpy release notes + opencv pip guide + Pillow releasenotes; Beken BK7252 + bdk_rtt; `intx82/a9-v720#51`. URLs inline en §§1–5.

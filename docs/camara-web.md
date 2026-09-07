# Cámara web passthrough — HOWTO Pi (una página)

Objetivo: ver la cámara en el navegador vía gateway `:8090` + esta página
(`static/index.html`). Sin galería ni grabaciones (eso va en #9).

## 1. Requisitos

- Pi con `wlan0` libre y cámara en modo AP (`Nax_*`).
- Gateway: `scripts/ap_gateway.py` (puerto **8090**, ver `docs/gateway-8090.md`).
- Nginx Proxy Manager (NPM) en red host, puertos 80/443, como frontal.
- El gateway **no** sirve estáticos: esta página debe servirse en el **mismo
  origen** que el proxy `/dev/*` (vía NPM o un frontal de prueba).

## 2. Asociar `wlan0` a la cámara y verificar

```bash
# Asociar (ajusta interfaz/SSID según tu Pi)
sudo iw dev wlan0 connect 'Nax_XXXX'
ip addr show wlan0            # esperas algo como 192.168.169.100
nc -vz 192.168.169.1 6123    # la cámara debe responder
```

Si `nc` falla: reasocia `wlan0`, comprueba que no haya otro perfil
(NetworkManager) robando la interfaz y reintenta una vez.

## 3. Levantar el gateway `:8090`

```bash
PYTHONPATH=src /tmp/v720fp/bin/python scripts/ap_gateway.py --port 8090
curl -s http://127.0.0.1:8090/dev/list   # → [{"uid":"ap-camera",...}]
```

## 4. Frontal NPM + AdGuard (misma receta validada en `docs/npm-cam-route.md`)

1. Estático: `python3 -m http.server 8000 --directory static`
2. AdGuard > DNS rewrites: `camara.lan` → `192.168.0.204`
   (el PC debe usar AdGuard como DNS; si no, entrada hosts)
3. NPM Proxy Host `camara.lan` → `192.168.0.204:8000` con Access List
   (auth) + *Custom location* `/dev` → `192.168.0.204:8090`

La página usa URLs **relativas** (`dev/list`, `dev/<uid>/live`,
`dev/<uid>/snapshot`): funciona siempre que `/` y `/dev/*` compartan
origen, que es justo lo que arma esta receta.

## 5. Probar

```bash
curl -s http://camara.lan/dev/list
curl -o s.jpg http://camara.lan/dev/ap-camera/snapshot && file s.jpg  # → JPEG
```

En el navegador (PC en LAN): abre `http://camara.lan/` → ves la(s) cámara(s)
con `<img>` del MJPEG en directo + enlaces *Ver en directo* y *Snapshot*.

## 6. Puertos ocupados en esta Pi (no pisar)

| Puerto  | Servicio              |
|---------|-----------------------|
| 80/443  | nginx-proxy-manager   |
| 53      | AdGuard               |
| 8080    | bentopdf              |
| 8081    | metube                |
| 8082    | it-tools              |
| 8083    | stirling              |
| **8090**| **gateway cámara**    |

## 7. Notas

- **Un visor a la vez:** un segundo `/live` responde `503 camera busy`.
  La página lo indica y el `img` muestra el aviso; recarga cuando se libere.
- Cada sesión cierra vídeo (`code 0`) al soltar el cliente (ver #10); si ves
  `502 no frame in time`: reset físico de cámara + un reintento.
- **AdGuard (puerto 53) reservado** para el futuro modo STA (ver #8):
  no exponer ni mover el 53; el frontal web sigue en NPM (80/443).
- Galería/grabaciones: fuera de alcance, van en #9.

# Security Camera Network

## Project Goal

This project is a DIY security camera network prototype.

- Phones can stream live JPEG frames through FastAPI WebSockets.
- A Linux server stores browser-camera and RTSP-camera recordings.
- A web dashboard views cameras, changes camera policy, and browses clips.

This project does not record audio, use a database, or require authentication yet. Recordings are saved as local video files under `recordings/` and exposed through the Recordings page.

Camera Mode includes simple controls for FPS, JPEG quality, and capture resolution. Higher settings can look better, but they also use more phone CPU and LAN bandwidth.

## TopTek Hikvision DVR RTSP Support

The `topteksystem` branch adds four server-side Hikvision DVR cameras. They appear in `/viewer` beside normal phone/browser cameras with an `RTSP` source badge.

The default RTSP config template is:

```text
server/config/rtsp_cameras.example.json
```

The local override path is ignored by Git so the DVR password is not committed:

```text
server/config/rtsp_cameras.json
```

The committed example config uses `HIKVISION_DVR_PASSWORD` in each URL. Set it before starting the server:

```bash
export HIKVISION_DVR_PASSWORD='your-dvr-password'
```

On PowerShell:

```powershell
$env:HIKVISION_DVR_PASSWORD = 'your-dvr-password'
```

The configured TopTek substreams are:

| Camera | Dashboard stream | Future main stream |
| --- | --- | --- |
| TopTek Camera 1 | `/Streaming/Channels/102` | `/Streaming/Channels/101` |
| TopTek Camera 2 | `/Streaming/Channels/202` | `/Streaming/Channels/201` |
| TopTek Camera 3 | `/Streaming/Channels/302` | `/Streaming/Channels/301` |
| TopTek Camera 4 | `/Streaming/Channels/402` | `/Streaming/Channels/401` |

Substreams are used by default because they are lighter for a four-camera dashboard grid. Main streams are better reserved for future high-quality recording work.

Useful RTSP API endpoints:

```text
GET  /api/rtsp-cameras
POST /api/rtsp-cameras/reload
POST /api/rtsp-cameras/test
```

Example RTSP test request:

```bash
curl -X POST http://127.0.0.1:8000/api/rtsp-cameras/test \
  -H 'Content-Type: application/json' \
  -d '{"camera_id":"toptek_cam_1"}'
```

Before deploying on Ubuntu, confirm the server can reach the DVR:

```bash
ping 192.168.0.222
nc -vz 192.168.0.222 554
```

If the DVR remains on the `192.168.0.x` subnet while the Ubuntu server is primarily on another subnet, add a temporary address on the correct interface:

```bash
sudo ip addr add 192.168.0.200/24 dev enp1s0
```

Replace `enp1s0` with the actual Ubuntu network interface. Long term, move the DVR to `192.168.1.222` so it lives on the same LAN as the rest of the deployment.

OpenCV is required for RTSP capture and server-side detection. The Python dependency is already listed in `server/requirements.txt`; on Ubuntu, install FFmpeg/OpenCV runtime libraries if the installed `opencv-python-headless` wheel cannot open RTSP streams.

## Windows Local Testing

1. Go into the server folder:

   ```powershell
   cd server
   ```

2. Activate the virtual environment:

   ```powershell
   .venv\Scripts\Activate.ps1
   ```

3. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

4. Run the dev server:

   ```powershell
   uvicorn app.main:app --reload
   ```

5. Open:

   ```text
   http://127.0.0.1:8000
   ```

## Ubuntu Server Testing

1. Go into the repo:

   ```bash
   cd ~/securitycameranetwork
   ```

2. Pull the latest code:

   ```bash
   git pull
   ```

3. Go into the server folder:

   ```bash
   cd server
   ```

4. Activate the virtual environment:

   ```bash
   source .venv/bin/activate
   ```

5. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

6. Run the server on the LAN:

   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

7. Open this address on a phone or laptop:

   ```text
   http://192.168.1.199:8000
   ```

## Testing Flow

1. Open `http://192.168.1.199:8000/camera` on a phone.
2. Enter a camera ID like `front-door`.
3. Tap Start Camera.
4. Open `http://192.168.1.199:8000/viewer` on another device.
5. Confirm the live feed appears.

Some mobile browsers may block camera access on plain HTTP over a LAN. If camera permission fails on `http://192.168.1.199`, HTTPS may be required later through Tailscale, a local certificate, or another secure setup.

If the camera page says `Camera access is not available in this browser context`, the browser is blocking `getUserMedia`. This usually happens on a phone when using plain HTTP with a LAN IP address. Try the page from `http://127.0.0.1:8000` on the same machine as the server, or use HTTPS for phone testing.

## Ubuntu HTTPS Setup with Caddy

Chrome requires HTTPS for camera access from a phone. On the Ubuntu server, run FastAPI on localhost and let Caddy serve HTTPS on the LAN.

1. Install or update the app:

   ```bash
   cd ~/securitycameranetwork
   git pull
   cd server
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Install the Uvicorn systemd service:

   ```bash
   sudo cp ~/securitycameranetwork/deploy/securitycameranetwork.service /etc/systemd/system/securitycameranetwork.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now securitycameranetwork
   sudo systemctl restart securitycameranetwork
   ```

3. Install Caddy:

   ```bash
   sudo apt update
   sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
   sudo apt update
   sudo apt install -y caddy
   ```

4. Install the Caddy config:

   ```bash
   sudo cp ~/securitycameranetwork/deploy/Caddyfile /etc/caddy/Caddyfile
   sudo systemctl reload caddy
   ```

5. Open the HTTPS site:

   ```text
   https://192.168.1.199
   ```

Because this uses Caddy's internal certificate authority, phones and laptops need to trust the Caddy local root certificate before Chrome will accept the HTTPS page without warnings.

To copy the Caddy root certificate somewhere you can download it:

```bash
sudo cp /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt ~/securitycameranetwork/server/app/static/caddy-root.crt
sudo chmod 644 ~/securitycameranetwork/server/app/static/caddy-root.crt
```

Then open this on the phone and install/trust the certificate:

```text
https://192.168.1.199/static/caddy-root.crt
```

After the certificate is trusted, use:

```text
https://192.168.1.199/camera
```

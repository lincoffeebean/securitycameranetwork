# TopTek Hikvision WebRTC Deployment

This deployment uses two media paths:

- FastAPI WebSockets for camera metadata, status, controls, detection events, and recording events.
- MediaMTX for RTSP-to-WebRTC live video playback in the browser.

The backend RTSP workers still decode frames for detection and recording. Viewer tiles for TopTek RTSP cameras use WebRTC `<video>` elements instead of JPEG frame refreshes.

## Network

The DVR is currently reachable at:

```text
192.168.0.222
```

Confirm the Ubuntu box can reach it:

```bash
ping 192.168.0.222
nc -vz 192.168.0.222 554
```

If the DVR is still on the `192.168.0.x` subnet and Ubuntu is primarily on `192.168.1.x`, add a temporary address:

```bash
sudo ip addr add 192.168.0.200/24 dev enp1s0
```

Replace `enp1s0` with the actual interface name. Long term, move the DVR to:

```text
192.168.1.222
```

## DVR Encoding

Use the substreams for dashboard viewing:

| Camera | Substream | Future main stream |
| --- | --- | --- |
| TopTek Camera 1 | `/Streaming/Channels/102` | `/Streaming/Channels/101` |
| TopTek Camera 2 | `/Streaming/Channels/202` | `/Streaming/Channels/201` |
| TopTek Camera 3 | `/Streaming/Channels/302` | `/Streaming/Channels/301` |
| TopTek Camera 4 | `/Streaming/Channels/402` | `/Streaming/Channels/401` |

Required DVR settings:

- H.264
- No H.265/HEVC
- No H.264+ or H.265+
- No Smart Codec
- No Hik-Connect/platform stream encryption

Camera 1 has been verified as H.264, 960x480, 12 fps.

## Test RTSP Directly

```bash
ffmpeg -rtsp_transport tcp \
  -analyzeduration 10000000 \
  -probesize 10000000 \
  -i "rtsp://admin:toptek20%3F@192.168.0.222:554/Streaming/Channels/102" \
  -t 10 \
  -f null -
```

Repeat with channels `202`, `302`, and `402`.

## Install MediaMTX

Download the current Linux amd64 release:

```bash
mkdir -p ~/mediamtx
cd ~/mediamtx
python3 - <<'PY'
import json
import urllib.request

release = json.load(urllib.request.urlopen("https://api.github.com/repos/bluenviron/mediamtx/releases/latest"))
asset = next(item for item in release["assets"] if "linux_amd64.tar.gz" in item["name"])
print(asset["browser_download_url"])
PY
```

Use the printed URL:

```bash
curl -L -o /tmp/mediamtx.tar.gz "PASTE_RELEASE_ASSET_URL_HERE"
tar -xzf /tmp/mediamtx.tar.gz -C ~/mediamtx
chmod +x ~/mediamtx/mediamtx
```

The project config is:

```text
deploy/mediamtx.yml
```

It defines four paths: `toptek_cam_1`, `toptek_cam_2`, `toptek_cam_3`, and `toptek_cam_4`.

## Run MediaMTX Manually

From the Ubuntu repo:

```bash
~/mediamtx/mediamtx ~/topteksecurity/deploy/mediamtx.yml
```

In another terminal, open the built-in playback pages:

```text
http://192.168.1.199:8889/toptek_cam_1/
http://192.168.1.199:8889/toptek_cam_2/
http://192.168.1.199:8889/toptek_cam_3/
http://192.168.1.199:8889/toptek_cam_4/
```

The WHEP endpoints used by the dashboard are:

```text
http://192.168.1.199:8889/toptek_cam_1/whep
http://192.168.1.199:8889/toptek_cam_2/whep
http://192.168.1.199:8889/toptek_cam_3/whep
http://192.168.1.199:8889/toptek_cam_4/whep
```

## Run MediaMTX With systemd

The example service is:

```text
deploy/mediamtx.service
```

Install it:

```bash
sudo cp ~/topteksecurity/deploy/mediamtx.service /etc/systemd/system/mediamtx.service
sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx
sudo systemctl status mediamtx
```

View logs:

```bash
journalctl -u mediamtx -f
```

## FastAPI Dashboard

Run the FastAPI app on the Ubuntu LAN:

```bash
cd ~/topteksecurity/server
source /home/expiredsession/securitycameranetwork/server/.venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open:

```text
http://192.168.1.199:8000/viewer
```

Useful endpoints:

```text
GET /api/rtsp-cameras
GET /api/webrtc-cameras
GET /api/mediamtx/status
POST /api/rtsp-cameras/reload
POST /api/rtsp-cameras/test
```

If MediaMTX is not on the same host or uses a different port, set these before starting FastAPI:

```bash
export MEDIAMTX_HOST=192.168.1.199
export MEDIAMTX_WEBRTC_PORT=8889
export MEDIAMTX_WEBRTC_SCHEME=http
```

## Troubleshooting

- `401 Unauthorized`: DVR username/password or password URL encoding is wrong.
- H.264 PPS/SPS/CABAC decode errors: DVR codec, smart codec, or platform encryption is still wrong.
- FFmpeg works but WebRTC fails: check `journalctl -u mediamtx -f` or the manual MediaMTX terminal output.
- DVR unreachable: confirm the temporary `192.168.0.200/24` address is still on the Ubuntu interface.
- Dashboard shows RTSP online but video unavailable: FastAPI can decode the RTSP stream, but the browser cannot reach MediaMTX on port `8889`.
- Browser blocks playback from HTTPS dashboard to HTTP MediaMTX: serve MediaMTX over HTTPS or keep the dashboard on HTTP for local LAN testing.

Do not expose the DVR ports or MediaMTX directly to the internet. Use VPN/Tailscale or a secured reverse proxy for remote access.

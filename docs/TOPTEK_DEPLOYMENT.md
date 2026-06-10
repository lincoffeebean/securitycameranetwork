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

The DVR password that existed in earlier private-branch commits should be considered exposed and should be rotated on the DVR. Store the current password only in environment variables or ignored local config files.

## Test RTSP Directly

```bash
export HIKVISION_DVR_PASSWORD_ENCODED='url-encoded-dvr-password'
ffmpeg -rtsp_transport tcp \
  -analyzeduration 10000000 \
  -probesize 10000000 \
  -rw_timeout 15000000 \
  -i "rtsp://admin:${HIKVISION_DVR_PASSWORD_ENCODED}@192.168.0.222:554/Streaming/Channels/102" \
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

The Hikvision DVR currently advertises H.264 packetization mode `0`. MediaMTX v1.19 rejects that mode when it pulls the DVR directly, so the deployment config uses `runOnInit` to start FFmpeg for each path. FFmpeg pulls the DVR over RTSP/TCP, copies the H.264 video without re-encoding, and republishes it back into MediaMTX as the browser-facing path.

The FFmpeg commands include `-rw_timeout 15000000` so a dead DVR read exits instead of leaving MediaMTX with a stale path. MediaMTX then restarts the publisher through `runOnInitRestart`.

The committed MediaMTX config expects this environment variable:

```bash
export HIKVISION_DVR_PASSWORD_ENCODED='url-encoded-dvr-password'
```

For the FastAPI login gate and RTSP workers, also set:

```bash
export HIKVISION_DVR_PASSWORD='plain-dvr-password'
export SCN_SESSION_SECRET='generate-a-long-random-value'
```

On Ubuntu, place these in an ignored local file such as `server/.env` and source it before manual runs, or install the systemd services which read the environment file.

## Run MediaMTX Manually

From the Ubuntu repo:

```bash
set -a
. ~/topteksecurity/server/.env
set +a
~/mediamtx/mediamtx ~/topteksecurity/deploy/mediamtx.yml
```

MediaMTX does not expose these paths as normal GET pages in this config. The WHEP endpoints used by the FastAPI dashboard are:

```text
http://192.168.1.199:8889/toptek_cam_1/whep
http://192.168.1.199:8889/toptek_cam_2/whep
http://192.168.1.199:8889/toptek_cam_3/whep
http://192.168.1.199:8889/toptek_cam_4/whep
```

A browser or `curl` GET to these URLs is not a valid playback test. WHEP clients POST an SDP offer to the endpoint; the FastAPI viewer does this automatically.

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
set -a
. ./.env
set +a
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
- `unsupported packetization mode: 0` in MediaMTX logs: use the committed FFmpeg republish config instead of direct `source: rtsp://...` paths.
- FFmpeg works but WebRTC fails: check `journalctl -u mediamtx -f` or the manual MediaMTX terminal output.
- WHEP POST returns `404` with `no stream is available`: the WHEP endpoint exists, but MediaMTX has no active publisher for that camera path. Check the FFmpeg publisher processes, confirm `ping 192.168.0.222` and `nc -vz 192.168.0.222 554`, then restart MediaMTX.
- DVR unreachable: confirm the temporary `192.168.0.200/24` address is still on the Ubuntu interface.
- Dashboard shows RTSP online but video unavailable: FastAPI can decode the RTSP stream, but the browser cannot reach MediaMTX on port `8889`.
- Browser blocks playback from HTTPS dashboard to HTTP MediaMTX: serve MediaMTX over HTTPS or keep the dashboard on HTTP for local LAN testing.

Do not port-forward or publicly expose FastAPI, the DVR, MediaMTX, or recording files. Use LAN/Tailscale/VPN access, or a secured reverse proxy you trust.

# Security Camera Network

Turn spare phones into cameras for a small, self-hosted security camera network. A FastAPI server relays live views, coordinates human-triggered recordings, and stores clips on your own Linux machine.

> **Project status:** early but functional. It is intended for experimentation on a trusted home LAN, not public Internet exposure.

## Features

- Use a modern phone browser as a camera—no mobile app required
- View multiple live cameras from one dashboard
- Browser-based pose detection with a server-side OpenCV fallback
- Automatic or manually triggered video clips
- Adjustable idle and active resolution, FPS, JPEG quality, zoom, duration, and cooldown
- Filesystem recording storage with camera/date/hour browsing
- Interactive Ubuntu setup and management commands
- Optional systemd startup and Caddy HTTPS

## Quick Start

On a fresh Ubuntu machine:

```bash
git clone https://github.com/lincoffeebean/securitycameranetwork.git
cd securitycameranetwork
./setup.sh
```

Follow the setup wizard. It creates the Python environment, installs dependencies, writes the local configuration, optionally installs a systemd service, starts the server, and displays its LAN address:

```text
[OK] Security Camera Network is running

Dashboard:
http://192.168.1.199:8000

Camera:
http://192.168.1.199:8000/camera
```

Any phone, laptop, or tablet on the same LAN/Wi-Fi can open that address. `127.0.0.1` only works on the server itself; addresses such as `192.168.x.x` and `10.x.x.x` are reachable by other devices on the local network.

Run `./setup.sh` again to reconfigure or repair an installation. Existing recordings are not removed.

## Using a Phone as a Camera

1. Open the `/camera` URL displayed by setup.
2. Enter a camera ID such as `front-door`.
3. Allow camera access and select **Start Camera**.
4. Open the dashboard's **View Mode** on another device.

Browsers generally require HTTPS or localhost before granting camera access. Simple HTTP may work in some environments, but choose **LAN + HTTPS** during setup if a phone refuses camera permission.

## Managing the Server

Run `./securitycam` for an interactive menu, or use commands directly:

```bash
./securitycam start
./securitycam stop
./securitycam restart
./securitycam status
./securitycam logs
./securitycam recordings
./securitycam config
./securitycam update
./securitycam uninstall
```

When systemd is installed, these commands control the system service and read its journal. Otherwise they use a small local background process and `.securitycam/server.log`.

`update` refuses to proceed when tracked source files have local changes, pulls with `git pull --ff-only`, updates Python dependencies, and restarts the server only if it was previously running.

`uninstall` removes the systemd service and machine-local configuration after confirmation. It preserves recordings and the repository.

## Updating

From the repository directory, run:

```bash
./securitycam update
```

Updates are fast-forward-only and stop rather than overwrite local source changes.

## Configuration

Setup writes `.securitycam/config.json`, which is intentionally ignored by Git. Example:

```json
{
  "host": "0.0.0.0",
  "port": 8000,
  "recordings_path": "/home/user/securitycameranetwork/recordings",
  "max_storage_gb": 10.0,
  "https": false,
  "start_on_boot": true
}
```

Run `./securitycam config` to change it safely. The storage limit is currently saved for administration and display purposes; automatic deletion is not enabled, so recordings are never silently removed.

Camera connections, labels, and per-camera tuning remain in memory and reset when the server restarts.

## Recordings

Phones create clips with the browser's `MediaRecorder` API and upload them to the server. Clips are stored under the configured recordings directory in one folder per camera. Open `/recordings-browser` or run `./securitycam recordings` to inspect them.

## How It Works

```text
Phone camera ── JPEG frames / clip uploads ──> FastAPI WebSocket server
                                                   │
Viewer dashboard <── frames, state, and controls ──┘
                                                   │
                                      Filesystem recordings
```

The implementation deliberately stays small: one FastAPI application, static HTML/JavaScript pages, WebSockets, in-memory live state, and filesystem clips. There is no database, Redis, Docker, Node build step, or cloud dependency.

## Ubuntu and Linux

The supported installer targets Ubuntu with Bash, Python 3.10+, and `apt`. If startup on boot is enabled, setup creates `/etc/systemd/system/securitycameranetwork.service`, runs it as the installing user, and enables restart-on-failure.

Setup functionally tests Python virtual-environment support before installation. On Ubuntu/Debian, it offers to install `python3-venv` (or the active Python version's matching package) when needed and automatically repairs an incomplete `.venv` from a previous attempt.

Simple LAN mode listens on `0.0.0.0` using the selected port. HTTPS mode binds FastAPI to localhost and uses Caddy as the LAN-facing reverse proxy. Caddy's internal CA certificate must be trusted on each client device.

If UFW is active, setup opens only the selected LAN HTTP port or HTTPS port 443. It does not configure router port forwarding or expose the server publicly.

## Development

### Ubuntu/Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r server/requirements.txt
uvicorn app.main:app --app-dir server --reload
```

### Windows

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r server\requirements.txt
uvicorn app.main:app --app-dir server --reload
```

Then open <http://127.0.0.1:8000>. Manual development does not require systemd or a generated configuration file; the app defaults to the repository's `recordings/` directory.

## Routes

- `/` — home dashboard
- `/camera` — phone camera mode
- `/viewer` — live viewer and camera controls
- `/recordings-browser` — saved clip browser
- `/health` — service health
- `/api/recordings` — recording metadata
- `/ws/camera/{camera_id}` — camera WebSocket
- `/ws/viewer` — viewer WebSocket

## Security and Network Notes

The current system has **no authentication or user management**. Anyone who can reach the server can view cameras, change settings, trigger recordings, and access saved clips.

Use it only on a network you trust. Do not port-forward it directly to the public Internet. For remote access, use a properly configured VPN or secure tunnel; HTTPS alone does not add user authentication.

## Contributing

Issues and focused pull requests are welcome. Keep changes understandable and preserve the project's lightweight, self-hosted architecture. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and testing guidance, and [SECURITY.md](SECURITY.md) for private vulnerability reporting.

## Project Status

The project is pre-1.0 and under active development. Live viewing, pose-based detection, recording, clip browsing, installation, and service management are implemented. Authentication, user accounts, enforced storage quotas, and hardened Internet-facing deployment are not.

## License

No license file has been added yet. Until one is chosen, normal copyright restrictions apply.

# Security Camera Network

## Project Goal

This project is a DIY security camera network prototype.

- Phones will eventually record and upload video chunks.
- A Linux server will store recordings.
- A web dashboard will eventually view cameras and clips.

The current MVP only streams live JPEG frames through FastAPI WebSockets. It does not save video, record audio, use a database, or require authentication.

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

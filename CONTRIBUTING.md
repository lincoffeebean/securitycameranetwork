# Contributing

Thanks for helping improve Security Camera Network. Keep changes focused and understandable; this is intentionally a small FastAPI and browser-JavaScript project.

## Development setup

```bash
git clone https://github.com/lincoffeebean/securitycameranetwork.git
cd securitycameranetwork
git switch -c your-feature-name
python3 -m venv .venv
source .venv/bin/activate
pip install -r server/requirements.txt
uvicorn app.main:app --app-dir server --reload
```

On Windows, activate with `.venv\Scripts\Activate.ps1`; the remaining commands are the same.

## Repository structure

- `server/app/main.py` — FastAPI routes, WebSockets, detection, and recording storage
- `server/app/static/` — camera, viewer, recordings, and home pages
- `setup.sh` — interactive Ubuntu/Linux installer
- `securitycam` — server management CLI
- `recordings/` — local clips; contents are ignored

## Before opening a pull request

1. Run `python -m py_compile server/app/main.py`.
2. Run `bash -n setup.sh securitycam` when changing either shell script.
3. Start the server and check `/health`, `/camera`, `/viewer`, and `/recordings-browser`.
4. For WebSocket changes, connect both a camera and viewer and confirm frames, state, and controls still flow.
5. Describe what changed and any limitations in the pull request.

Do not commit recordings, `.venv`, `.securitycam`, environment files, logs, Graphify output, credentials, or machine-specific paths. Never discard another contributor's local changes in update or setup code.

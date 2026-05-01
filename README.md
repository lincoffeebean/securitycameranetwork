# Security Camera Network

## Project Goal

This project is the starting point for a DIY security camera network.

- Phones will eventually record and upload video chunks.
- A Linux server will store recordings.
- A web dashboard will eventually view cameras and clips.

For now, the project only includes a minimal FastAPI backend health check.

## Run the Backend on Windows PowerShell

1. Open the repo in VS Code:

   ```powershell
   code .
   ```

2. Go into the server folder:

   ```powershell
   cd server
   ```

3. Create a virtual environment:

   ```powershell
   python -m venv .venv
   ```

4. Activate it:

   ```powershell
   .venv\Scripts\Activate.ps1
   ```

5. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

6. Run the dev server:

   ```powershell
   uvicorn app.main:app --reload
   ```

7. Open:

   ```text
   http://127.0.0.1:8000
   ```

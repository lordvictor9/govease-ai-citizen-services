# GovEase Python API

FastAPI backend for optional AI guidance, PIN directory lookup, and email-OTP account creation. See the project [README](../README.md) for setup and disclosure details.

```powershell
python -m venv .venv
./.venv/Scripts/Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn main:app --reload --port 8000
```

Use a persistent data volume for the SQLite database in production. Configure an HTTPS mail relay and keep `.env` private. `GET /api/health` reports whether each integration has the required environment variables; it does not validate the provider credentials.


"""GovEase API: optional Python services for the static citizen guide.

The API only provides AI guidance, public PIN directory lookup, and email OTP
account creation. It does not connect to government case-management systems.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel, Field


APP_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("GOVEASE_DATABASE_PATH", str(APP_DIR / "govease.sqlite3")))
OTP_TTL_SECONDS = 5 * 60
OTP_RESEND_SECONDS = 60
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_CORS = [
    "https://lordvictor9.github.io",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
]

app = FastAPI(title="GovEase Citizen Guide API", version="1.0.0")
origins = [item.strip() for item in os.getenv("GOVEASE_CORS_ORIGINS", ",".join(DEFAULT_CORS)).split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)


class AssistantRequest(BaseModel):
    query: str = Field(min_length=1, max_length=280)


class OTPRequest(BaseModel):
    name: str = Field(min_length=1, max_length=70)
    email: str = Field(min_length=3, max_length=254)


class OTPVerifyRequest(OTPRequest):
    otp: str = Field(pattern=r"^\d{6}$")


def _db() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS otp_challenges (
            email TEXT PRIMARY KEY,
            code_hash TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            last_sent_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        );
        """
    )
    return connection


@contextmanager
def _database():
    connection = _db()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _normalise_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    return email


def _otp_hash(email: str, code: str) -> str:
    secret = os.getenv("GOVEASE_OTP_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(status_code=503, detail="Email verification is not configured yet.")
    value = f"{email}:{code}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), value, hashlib.sha256).hexdigest()


def _send_email_otp(email: str, name: str, code: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    from_address = os.getenv("SMTP_FROM_EMAIL", "").strip()
    if not all((host, username, password, from_address)):
        raise HTTPException(status_code=503, detail="Email verification is not configured yet.")

    message = EmailMessage()
    message["Subject"] = "Your GovEase verification code"
    message["From"] = from_address
    message["To"] = email
    message.set_content(
        f"Hello {name},\n\nYour GovEase email verification code is {code}. "
        f"It expires in 5 minutes. If you did not request this code, you can ignore this email.\n\n"
        "GovEase is an independent citizen-service guide, not a government department."
    )
    port = int(os.getenv("SMTP_PORT", "587"))
    timeout = 15
    try:
        if os.getenv("SMTP_USE_SSL", "false").lower() == "true":
            with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context()) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(username, password)
                server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        # Do not include SMTP credentials or user email in server error details.
        raise HTTPException(status_code=502, detail="The email provider could not deliver the code. Try again shortly.") from exc


def _looks_sensitive(text: str) -> bool:
    return bool(
        re.search(r"\b(aadhaar|aadhar|otp|password|passcode|bank account|account number)\b", text, re.I)
        or re.search(r"\d{8,}", text)
        or re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", text)
    )


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "ai_configured": bool(os.getenv("OPENAI_API_KEY")),
        "pincode_configured": bool(os.getenv("DATA_GOV_IN_API_KEY") and os.getenv("DATA_GOV_IN_RESOURCE_ID")),
        "email_otp_configured": all(os.getenv(key) for key in ("GOVEASE_OTP_SECRET", "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL")),
    }


@app.post("/api/assistant")
def assistant(request: AssistantRequest) -> dict[str, str]:
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="Ask a short question about a public service.")
    if _looks_sensitive(query):
        raise HTTPException(status_code=400, detail="Please remove personal numbers, email addresses, passwords, and OTPs from the question.")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="The AI guide is not configured yet.")

    instructions = (
        "You are the friendly GovEase citizen-service guide for India. Use short, calm, plain-language steps. "
        "Give general navigation help only. Never claim to be a government service, submit applications, check case status, "
        "know live officer availability, or confirm changing fees, eligibility, deadlines, or required documents. "
        "Do not invent portals, contacts, or procedures. Ask the person to verify current details on the relevant official portal. "
        "Do not request Aadhaar, OTPs, passwords, bank details, phone numbers, or other sensitive data. "
        "If a bribe is demanded or someone is in immediate danger, direct the user to the official local vigilance or emergency channel; "
        "do not promise that GovEase sends a report."
    )
    try:
        client = OpenAI(api_key=api_key, timeout=20, max_retries=1)
        response = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-6.2-sol"),
            instructions=instructions,
            input=query,
            max_output_tokens=420,
            store=False,
        )
        answer = (response.output_text or "").strip()
        if not answer:
            raise HTTPException(status_code=502, detail="The AI guide returned an empty answer. Try again.")
        return {"answer": answer, "source": "AI-generated general guidance; verify on the linked official portal."}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="The AI service is temporarily unavailable. Use the built-in guide and official links.") from exc


@app.get("/api/pincode")
def pincode(pin: str = Query(pattern=r"^\d{6}$")) -> dict[str, object]:
    api_key = os.getenv("DATA_GOV_IN_API_KEY", "").strip()
    resource_id = os.getenv("DATA_GOV_IN_RESOURCE_ID", "").strip()
    if not api_key or not resource_id:
        raise HTTPException(status_code=503, detail="The data.gov.in PIN directory is not configured yet.")
    params = urlencode({"api-key": api_key, "format": "json", "filters[pincode]": pin, "limit": "10"})
    url = f"https://api.data.gov.in/resource/{resource_id}?{params}"
    try:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "GovEase-Citizen-Guide/1.0"})
        with urlopen(request, timeout=12) as response:
            import json
            payload = json.loads(response.read(1_000_000).decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        raise HTTPException(status_code=502, detail="The official PIN directory is temporarily unavailable.") from exc

    records = []
    for row in payload.get("records", [])[:10]:
        records.append({
            "office": str(row.get("officename") or row.get("office_name") or "Post office"),
            "district": str(row.get("districtname") or row.get("district_name") or ""),
            "state": str(row.get("statename") or row.get("state_name") or ""),
            "pincode": str(row.get("pincode") or pin),
        })
    return {"records": records, "source": "data.gov.in All India PIN Code Directory"}


@app.post("/api/auth/request-otp")
def request_otp(request: OTPRequest) -> dict[str, str]:
    email = _normalise_email(request.email)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Enter your name.")
    now = int(time.time())
    with _database() as connection:
        previous = connection.execute("SELECT last_sent_at FROM otp_challenges WHERE email = ?", (email,)).fetchone()
        if previous and now - previous["last_sent_at"] < OTP_RESEND_SECONDS:
            raise HTTPException(status_code=429, detail="Please wait a minute before requesting another code.")

    # Deliver before recording the challenge so failed deliveries cannot be verified.
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = _otp_hash(email, code)
    _send_email_otp(email, name, code)
    with _database() as connection:
        connection.execute(
            "INSERT INTO otp_challenges(email, code_hash, expires_at, last_sent_at, attempts) VALUES (?, ?, ?, ?, 0) "
            "ON CONFLICT(email) DO UPDATE SET code_hash=excluded.code_hash, expires_at=excluded.expires_at, last_sent_at=excluded.last_sent_at, attempts=0",
            (email, code_hash, now + OTP_TTL_SECONDS, now),
        )
    return {"message": "Verification code sent. It expires in 5 minutes."}


@app.post("/api/auth/verify-otp")
def verify_otp(request: OTPVerifyRequest) -> dict[str, str]:
    email = _normalise_email(request.email)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Enter your name.")
    expected_hash = _otp_hash(email, request.otp)
    now = int(time.time())
    with _database() as connection:
        challenge = connection.execute("SELECT * FROM otp_challenges WHERE email = ?", (email,)).fetchone()
        if not challenge or challenge["expires_at"] < now:
            raise HTTPException(status_code=400, detail="That code expired. Request a new one.")
        if challenge["attempts"] >= 5:
            connection.execute("DELETE FROM otp_challenges WHERE email = ?", (email,))
            connection.commit()
            raise HTTPException(status_code=429, detail="Too many attempts. Request a new code.")
        if not hmac.compare_digest(challenge["code_hash"], expected_hash):
            connection.execute("UPDATE otp_challenges SET attempts = attempts + 1 WHERE email = ?", (email,))
            connection.commit()
            raise HTTPException(status_code=400, detail="That code does not match. Check the email and try again.")

        connection.execute(
            "INSERT INTO users(email, name, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET name=excluded.name",
            (email, name, now),
        )
        connection.execute("DELETE FROM otp_challenges WHERE email = ?", (email,))
        token = secrets.token_urlsafe(36)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        connection.execute("INSERT INTO sessions(token_hash, email, expires_at) VALUES (?, ?, ?)", (token_hash, email, now + SESSION_TTL_SECONDS))

    return {"access_token": token, "token_type": "Bearer", "name": name, "expires_in": str(SESSION_TTL_SECONDS)}


@app.get("/api/me")
def current_user(authorization: str | None = Header(default=None)) -> dict[str, str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in with an email code.")
    token = authorization[7:].strip()
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = int(time.time())
    with _database() as connection:
        row = connection.execute(
            "SELECT users.name, users.email FROM sessions JOIN users USING(email) WHERE token_hash = ? AND expires_at > ?",
            (token_hash, now),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="This session expired. Verify your email again.")
    return {"name": row["name"], "email": row["email"]}


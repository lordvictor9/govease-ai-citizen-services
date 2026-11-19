# GovEase AI — Citizen Service Guide

An independent India citizen-service prototype built with a responsive HTML/CSS/JavaScript website and an optional Python FastAPI backend.

## What’s included

- Searchable service guides, step-by-step preparation lists, visit reminders, Maps links, audio guidance, document helper, Bribe SOS contacts, and DigiLocker links.
- Email OTP account creation through the optional Python service. The backend stores verified email accounts in SQLite and issues a short-lived browser-session token.
- An optional OpenAI Responses API assistant. Its key stays on the Python server; prompts are checked for obvious personal details before forwarding.
- An optional six-digit PIN lookup using the Open Government Data Platform India directory.
- JavaScript demo fallbacks for OTP, AI guidance, and PIN lookup when no backend is configured.

## Important prototype boundaries

GovEase is not affiliated with a government department. Officer/queue status is sample data. The website does not submit government applications or connect to government case records. DigiLocker is an official-site link; this prototype does not access a DigiLocker account. Demo OTP codes are generated in the browser and are not emailed; demo completion does not create an account. Real email verification, AI responses, and PIN lookup require a separately deployed and configured backend.

## Run the static website

Open `outputs/govease-ai-citizen-services.html`, or serve the `outputs` folder from a local static server. `outputs/config.js` has an empty API base by default, so the demo stays self-contained.

## Configure the Python API

1. Install Python 3.10 or newer. From `backend/`, create a virtual environment and install `requirements.txt`.
2. Copy `.env.example` to `.env`; fill in a server-side OpenAI API key, SMTP mail relay details, a random `GOVEASE_OTP_SECRET` of 32 or more characters, and the data.gov.in API key and resource UUID for the All India PIN Code Directory.
3. Start the service with `uvicorn main:app --host 0.0.0.0 --port 8000` from `backend/`.
4. Set `window.GOVEASE_API_BASE` in `outputs/config.js` to the HTTPS origin of the deployed API, then publish the static site again.
5. Allow only the static website origin in `GOVEASE_CORS_ORIGINS`. Use HTTPS and a persistent volume for `GOVEASE_DATABASE_PATH`; otherwise accounts and sessions can be lost if the host replaces its filesystem.

Never place API keys, SMTP passwords, or the OTP secret in `config.js`, HTML, JavaScript, or a public repository. Keep the real `.env` out of version control. The API does not provide an officer-availability integration because no authorized live department feed is configured.

## API routes

- `GET /api/health` — indicates which integrations are configured.
- `POST /api/auth/request-otp` and `POST /api/auth/verify-otp` — email code and account creation.
- `GET /api/me` — read the active bearer-token session.
- `POST /api/assistant` — general AI service-navigation guidance.
- `GET /api/pincode?pin=751001` — matching public PIN directory records.

## Official integration references

- [OpenAI text generation / Responses API](https://developers.openai.com/api/docs/guides/text)
- [Open Government Data Platform India help](https://www.data.gov.in/help)
- [All India PIN Code Directory](https://www.data.gov.in/resource/all-india-pincode-directory-till-last-month)
- [India Post post office finder](https://www.indiapost.gov.in/locate-postoffice)


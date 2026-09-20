# Smart Serve Deployment

## Local
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```
Open http://127.0.0.1:5000.

## Production
Use Render/Railway/Fly.io with a persistent volume or PostgreSQL. Set all variables from `.env.example`.
Google OAuth redirect URI must exactly match `https://YOUR-DOMAIN/auth/google/callback`.
Use Razorpay Test Mode first, then switch to Live keys after end-to-end verification.

## What is real
- Browser GPS updates and distance matching
- Customer/provider request state transitions
- Chat and status polling
- Completion proof and customer verification
- Razorpay server-side order creation and signature verification
- Google OpenID Connect session
- Gemini AI analysis on the AI path only

## Required secrets
Do not commit `.env`, Gemini keys, Google secrets, or Razorpay secrets.

# SmartServe V7 — Live Service + Trust + Village Marketplace

## Core service lifecycle
Customer creates request → SmartServe finds online eligible providers in the customer's 6-digit PIN service area → customer compares profiles/trust/distance → customer selects one provider → selected provider accepts/declines → provider route → arrival proximity → customer confirmation PIN → IN_PROGRESS → completion proof → customer verification → Razorpay payment → warranty + review → provider earnings/reputation.

## V7 features
1. Verified provider badges (profile, phone, eKYC submitted/verified, experience).
2. Provider portfolio with customer-visible previous-work images.
3. Favorite/trusted providers.
4. Default 7-day workmanship warranty after a paid completed service.
5. Dispute center with operations alerts.
6. Scheduled booking with future date/time.
7. Repeat/AMC plans (weekly/monthly/quarterly).
8. Provider earnings ledger with gross, platform fee, net and payout state.
9. Local-language preference: English, Hindi, Kannada, Tamil, Telugu, Marathi.
10. Low-bandwidth mode that reduces visual/image load and can be toggled per account.
11. SOS + trusted contact during a live service.
12. Admin operations center for supply, active jobs, disputes and alerts.
13. Explainable baseline fraud detection (e.g. repeated cancellations).
14. Smart provider ranking using trust, rating, experience, profile completeness, completed work and proximity.

## Village / every-PIN model
SmartServe does not require a city database. Any syntactically valid 6-digit Indian PIN is accepted as a service-area identifier. When the India Postal geocoder is available, an approximate map point is stored. If geocoding is unavailable (weak rural internet/offline test), PIN-only matching still works for customer/provider accounts sharing the same PIN; the map simply waits for a usable GPS coordinate.

## Live map
The live-service page shows customer and provider markers and draws an OSRM road route when coordinates are available. If routing is unavailable it falls back to a straight connecting line.

## Safety
The customer's arrival confirmation code is private and is never returned to the provider's status API. Raw eKYC documents are stored privately and are never shown to customers; customers see only verification badges/status.

## Local Linux setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 app.py
```

Use test Razorpay credentials in `.env`. Never commit `.env`.

## Demo accounts
Customer: `customer@smartserve.demo` / `SmartServe@123`
Provider: `arjun.plumbing@smartserve.demo` / `SmartServe@123`
Admin: `admin@smartserve.demo` / `SmartServe@123`

For a clean local demonstration, run `python3 demo_setup.py`. It preserves the database and prepares the demo accounts/provider service-area state.

## Production architecture
For a serious production launch, move from SQLite to PostgreSQL, use Redis for Socket.IO/message queues and dispatch, object storage for portfolio/completion media, a proper routing provider, masked calling, payment webhooks, KYC vendor integration, rate limiting, background workers and an admin RBAC model.

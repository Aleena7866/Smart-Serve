# SmartServe — Real-Time Service Marketplace

SmartServe is a Flask service marketplace with AI-assisted diagnosis, nearby provider matching, provider request/accept flow, Socket.IO real-time status events, live browser GPS, Leaflet maps, route ETA, private chat, completion proof, customer verification and Razorpay payments.

## Core live flow

1. Customer logs in or uses Google OAuth.
2. Customer chooses any supported service.
3. SmartServe lists approved providers matching the service and customer radius.
4. Customer clicks **Request Provider**.
5. The selected provider receives a real-time Socket.IO request notification.
6. Provider accepts or rejects.
7. Acceptance updates the customer immediately.
8. Private chat becomes available.
9. Both browsers can share GPS. Coordinates are pushed to the service room in near real time.
10. Leaflet updates both markers and the line between them.
11. OSRM route data provides driving distance and ETA when both locations are available.
12. Provider starts the service, uploads completion proof, customer verifies, and Razorpay handles payment verification.
13. Completion/review closes the workflow.

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 app.py
```

Open `http://127.0.0.1:5000`.

## Demo credentials

See `DEMO_ACCOUNTS.md`.

## Google OAuth

Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `GOOGLE_REDIRECT_URI` in `.env`. The callback must exactly match the URI configured in Google Cloud.

For local testing, configure:

`http://localhost:5000/auth/google/callback`

or the exact host/port you use.

## Gemini

Set a valid `GEMINI_API_KEY`. The default model is `gemini-2.5-flash`.

## Razorpay

Set `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`. Use test keys for development. The server creates orders and verifies the payment signature before marking a request paid.

## Real-time deployment

The included Procfile uses one Gunicorn worker and many threads because Socket.IO rooms cannot safely be spread across independent Gunicorn workers without a message queue and sticky sessions. For horizontal scaling, add Redis as the Socket.IO message queue and configure sticky sessions/load balancing.


## SmartServe V2 — Real-time Dispatch Upgrade

The booking flow is now designed as a dispatch marketplace rather than a simple provider-selection form:

1. **Location consent + live GPS** — customer and provider locations come from the browser; stale locations are excluded from dispatch.
2. **Upfront quote** — service pricing provides a bounded INR estimate before matching.
3. **Live pre-booking map** — nearby online providers with fresh GPS are shown with distance and driving ETA.
4. **Broadcast matching** — up to five suitable providers receive the request simultaneously.
5. **Auto-timeout** — provider offers expire after a short response window.
6. **Reassignment** — unanswered requests move to another provider wave and the radius expands automatically.
7. **Atomic acceptance** — the backend prevents two providers from winning the same request.
8. **Live service tracking** — after acceptance, customer and provider see each other's fresh GPS and OSRM driving ETA.
9. **Chat + calling** — private Socket.IO chat is available after acceptance; a phone button is available when a phone number exists.
10. **Price negotiation** — either participant can propose a price; the other can accept or reject it.
11. **Completion proof + verification + payment** — the existing proof/verification/Razorpay gate remains part of the lifecycle.
12. **Delivery & Errands** — the marketplace now has a local pickup/drop-off service category for town-level errands.

### Important pilot limitation

This version is genuinely real-time at the application layer using Socket.IO, browser GPS and short-lived dispatch offers. It is **not yet a national-scale distributed marketplace**. SQLite + one Gunicorn worker is appropriate for a college/demo/pilot deployment. For production scale, move dispatch state to PostgreSQL + Redis and run dedicated workers.

### Production deployment

For the current Socket.IO threading architecture, keep a single Gunicorn worker:

```bash
gunicorn --workers 1 --threads 100 --timeout 120 app:app
```

Do not run multiple independent workers against the same in-memory Socket.IO setup.

### Gemini

Current defaults are:

```text
GEMINI_MODEL=gemini-3.6-flash
GEMINI_FALLBACK_MODEL=gemini-2.5-flash
```

Do not commit `.env` or API/OAuth/Razorpay secrets.


### PIN-code location mode (local testing / town-market fallback)
Customers and providers can enter an Indian 6-digit PIN code instead of relying on browser GPS. SmartServe resolves the PIN to an approximate locality coordinate and uses it for matching and maps. This is useful for desktop testing and low-GPS environments; production provider dispatch should still require fresh GPS before showing a provider as physically nearby.


## Local PIN test mode
For local development, identical customer/provider PIN codes are treated as the same service area. This removes desktop GPS/geocoder precision as a blocker. Production should switch back to GPS/address verification.


## SmartServe V4 — Customer-led provider choice

This version changes booking selection to customer choice. After a customer creates a service with a PIN code, SmartServe shows the currently available matching providers in that service area. The customer compares trust score, rating, profile, experience, completed jobs, masked mobile number and distance/ETA, opens provider profiles, and selects one. Providers are not sent accept/reject offers during discovery. The selected provider is notified only after the customer makes the choice.

Realtime chat, calling, negotiation, live tracking, verification and Razorpay payment flows are retained. See `SMARTSERVE_V4_CUSTOMER_CHOICE.md` for the architecture and testing flow.


## V6 Trust & Service Safety Upgrade
- Provider arrival confirmation with customer-only 6-digit code.
- Customer-visible completion proof gallery.
- Call controls on both sides with graceful phone-missing handling.
- Provider profile/review center and public all-reviews page.
- PIN-based locations remain stable for local testing; post-selection maps draw a live line when both locations are available.

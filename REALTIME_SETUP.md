# SmartServe real-time setup

- Flask-SocketIO provides low-latency request, chat, status and GPS events.
- Browser GPS sends coordinates about every 4 seconds while an active page is open.
- Leaflet renders both parties and a live line.
- OSRM is used for route distance/ETA; a straight-line fallback is shown if routing is unavailable.
- HTTP polling remains as a resilience fallback.

## Local
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 app.py
```
Open http://127.0.0.1:5000.

## Production
The included Procfile uses one Gunicorn worker with threads because Socket.IO rooms require one worker unless Redis/message-queue coordination and sticky sessions are configured. Flask-SocketIO documents the threaded + simple-websocket deployment option.

## Google OAuth
Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and an exact GOOGLE_REDIRECT_URI. Local callback can be http://localhost:5000/auth/google/callback; production should use the exact HTTPS callback configured in Google Cloud.

## Razorpay
Use test keys first. Orders are created server-side and signatures are verified server-side before marking a service paid. Configure capture/webhooks for production.


## REAL GPS ONLY
SmartServe does not seed or invent provider/customer coordinates. Demo accounts start without a location. Each browser must grant location permission; the page sends browser GPS approximately every 4 seconds. A location is shown as live only when it was updated within the last 20 seconds. For production, serve the app over HTTPS because browser geolocation requires a secure context (localhost is allowed for local development).

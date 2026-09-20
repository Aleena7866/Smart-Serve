# SmartServe V7.2 — Realtime Stability & Safety

## Fixed in this release
- SQLite `database is locked` resilience: WAL, 30s busy timeout, connection-level lock retries, short write transactions.
- Completion-proof upload no longer keeps a DB connection open while writing image files.
- Completion proof is served through an authorization-checked endpoint and shown on the customer dashboard/live service.
- Provider calling button uses the native `tel:` handoff; missing numbers show a clear message.
- Provider registration now has a multi-select service dropdown for all SmartServe services.
- Provider profile editing uses the same service catalog and synchronizes normalized `provider_services` with legacy `providers.skills`.
- Google account provider completion also supports service selection.
- Scheduled booking UI now distinguishes Now vs Schedule for later.
- Due scheduled bookings activate automatically via a lightweight scheduler plus opportunistic web-worker check.
- SOS includes a clear 112 emergency-services action and creates a SmartServe operations alert.
- Safety & Tools warranty selection now works per active service.
- Secure system diagnostic: `/api/system/diagnostic` (login required).

## Rural / village support
Any valid six-digit Indian PIN can be used as the service-area key. External geocoding improves maps and distance, but same-PIN matching does not require a predefined city list or successful geocoding.

## Important
Do not commit `.env`, payment secrets, OAuth secrets, or private eKYC files. Keep the existing `smartserve.db` when upgrading; migrations are additive.

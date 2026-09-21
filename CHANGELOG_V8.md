# SmartServe v8 — completion release

This release finishes the marketplace end-to-end. Everything below was implemented in the
project and exercised against a running server (see “Verified flows”).

## What changed

### 1. Provider skills → grouped service picker
* `templates/_skill_picker.html` — grouped, multi-select checkbox cards (category → services), used by
  **Register**, **Google sign-up completion** and **Profile & Verification**.
* Services carry `category`, `icon`, `sort_order` (additive migration, existing rows back-filled).
* Selections are stored in `provider_services` **and** mirrored into the legacy `providers.skills`
  string so matching, search chips and public profiles all stay in sync.

### 2. Design system
* `static/css/app-ui.css` rewritten: tokens (colour, radius, shadow, motion), dark mode, 3D buttons
  (press depth, hover lift), 3D text fields (inset depth + focus ring), card tilt/lift, staggered
  `reveal` entrance animation, `prefers-reduced-motion` respected, mobile/tablet breakpoints.
* `static/js/ui.js` — toasts, modal, `uiConfirm`, `uiPrompt`, `api()`, busy-button states, image
  fallbacks, tilt effect. All `alert()/prompt()/confirm()` dialogs replaced.
* Leaflet 1.9.4 and Socket.IO client 4.8.1 are vendored under `static/vendor/` so pages do not depend
  on third-party CDNs.

### 3. Two-way live GPS tracking
* `SmartTracker` (in `static/js/realtime.js`) drives every `.smart-map[data-request-id]`:
  live markers for **both** parties, accuracy circles, OSRM road route (or an honest dashed
  straight-line fallback), distance, ETA with source label (`road` / `estimate`), arrival hint.
* States: `live` (≤45 s), `stale` (≤120 s), `offline`, `approximate` (PIN-code only). No ETA is
  shown unless both parties have fresh coordinates — nothing is simulated.
* Browser `watchPosition` streams through the existing `share_location` socket event; permission
  denial, unsupported devices and tile/network loss all surface as visible status text.
* Authorisation: `/api/request-location/<id>`, `/service/<id>/live`, `join_request` and
  `share_location` only work for the request’s customer and provider (403 / `socket_error` otherwise).
* `templates/live_service.html` rebuilt on SmartTracker with lifecycle timeline, provider actions
  (accept → arrived → confirm code → proof), customer private code, SOS and dispute modals.

### 4 & 5. Profile photo and portfolio pipelines
* Single validated upload path (`_save_public_image`): JPG/JPEG/PNG/WEBP, magic-byte check via
  Pillow, safe UUID filenames, 5 MB limit, served via `/media/<file>` with traversal protection.
* `media_url()` Jinja global + `photo_url` on every provider JSON — used by dashboards, search
  cards, public profile, favourites, live map markers, admin console, with initials fallback.
* Portfolio: upload, list, customer-facing gallery (`/api/provider/<id>/portfolio`), owner-only delete.

### 6. Customer Safety Tools (`/features`)
* Trusted contacts CRUD (max 5), SOS with live GPS + trusted contact + **112** call button,
  Dispute Centre (typed reasons, history, admin resolution visible to user), Service Warranty
  (auto 7-day after verified payment, check per service, claim → dispute), repeat plans, favourites,
  language and low-bandwidth preferences. No dead buttons.

### 7. Provider Business Hub
* Profile & Verification, Portfolio, Reviews (rating distribution), Earnings (net/gross/fees,
  monthly, payout status, awaiting payments), services, availability — all real pages.

### 8–9. Data & security
* Additive migrations only (`database.py`), safe on existing installs.
* Every endpoint checks session + role + ownership; proof images are only served to participants;
  admin console requires admin role.

### 10–11. Responsive + microcopy
* All templates extend `base.html`; nav, dropdown, cards, grids collapse for tablet/mobile.
* Wording reviewed (e.g. repeat plans no longer promise reminders that don’t exist — due visits are
  surfaced on the dashboard with a one-tap rebook instead).

## Verified flows (run against `python3 app.py` with demo data)
Registration (customer & provider with grouped skills) · login/logout · profile edit with WEBP photo
· portfolio add (JPG) / reject (GIF, corrupt PNG) / delete · public profile · request creation
(quick mode) · nearby providers · customer selects provider · provider accepts · two-way GPS
(REST + socket) with live/stale/offline transitions · outsider blocked from room/API/page · arrival
check · arrived → confirmation code → in progress · completion proof → customer verification ·
negotiation propose/accept · payment verification (HMAC path, with test secret) → COMPLETED →
warranty ACTIVE → earnings record · review submit (shown on profile) · repeat plan create/duplicate
guard/cancel · favourites toggle/list · trusted contact add/remove · SOS (stored, admin console,
socket to admins, acknowledge → socket to user) · dispute open → admin resolve → visible to user ·
cancel request (owner only) · all pages render without template/JS syntax errors.

## v8.1 — integration + browser verification pass

### Razorpay & OSRM exercised end-to-end (against local protocol stubs)
The sandbox cannot reach api.razorpay.com / router.project-osrm.org, so both integrations were run
against local HTTP stubs that speak the same protocol, selected purely by environment variables:

* `RAZORPAY_BASE_URL` — **host only** (e.g. `https://api.razorpay.com`); the SDK appends `/v1/...`.
* `OSRM_BASE_URL` — host of any OSRM `route/v1/driving` server (default `https://router.project-osrm.org`).

Verified: order creation (amount in paise, `order_id` persisted, `payment_status` PENDING), auth on
order/verify (401 for provider / other customer / anonymous), signature verification (missing
fields, wrong order id, tampered or wrong-secret signature → 400 with state unchanged), successful
HMAC → COMPLETED/PAID, warranty ACTIVE, `provider_earnings` row, replay protection, and the browser
checkout flow (Pay button → dismiss / failed / tampered / success) driven through the real
`paySmartServe()` code with a stand-in for `checkout.js`. OSRM: road distance & ETA from the route
response, 20 s route cache, refetch when either party moves, graceful fallback to straight-line
estimate (`eta_source: estimate`) during an outage, automatic recovery, 5 s negative cache.

### Two-browser live tracking (Playwright: desktop customer + mobile provider + outsider)
Markers move on the other party's screen, distance/ETA update, OSRM outage shows dashed line +
"estimate (routing offline)", stale (>25 s) and offline (>2 min) states render correctly, denied
geolocation shows a clear message, outsider gets 403 on page + API and `socket_error` on room join.

### Fixes found by that run
* `realtime.js`: a GPS fix arriving inside the send throttle window was dropped, so a single move
  could stay invisible until the next fix — now the newest position is flushed when the window
  ends, and a 12 s heartbeat keeps the peer from seeing "last seen" during an active service.
* Route polyline: `dashArray` is now reset explicitly when switching from estimate back to a road
  route (Leaflet keeps the previous dash pattern otherwise).
* "Map tiles could not load" banner was cleared by Leaflet's `load` event even when every tile
  failed — now cleared only on a real `tileload`.
* Call buttons on both dashboards used `|tojson` inside a double-quoted `onclick`, producing an
  unparsable handler (`Unexpected end of input` on every dashboard load). Same for "Select provider"
  in `providers.html`. Moved values to `data-*` attributes.
* Mobile/tablet horizontal overflow removed: provider choice cards (`minmax(0,1fr)` tracks +
  wrapping actions), landing nav, register page decorative blob on sticky aside, `.sr-only` select,
  chat drawer overlay.
* Live page metrics stack vertically on phones; map status pills no longer sit under the zoom
  control; `TEMPLATES_AUTO_RELOAD=1` env toggle added for development.

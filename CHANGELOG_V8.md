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

Razorpay order creation and OSRM routing require outbound internet and real keys; both code paths
fail loudly with clear messages when unavailable and were exercised as far as the sandbox allowed.

# SmartServe V2 implementation audit

## What was wrong in the supplied build

### Booking
- Customer manually selected one provider before any dispatch system existed.
- There was no provider-offer table, response deadline, matching wave or atomic first-accept logic.
- Rejection simply moved the request to `REJECTED`; it did not automatically try another provider.
- Price was mostly a free-form provider amount; there was no structured upfront quote.

### Location
- GPS collection and Socket.IO existed, but location was not a complete dispatch primitive.
- Provider discovery could fall back to showing skill-matched providers without GPS, which is unsafe for a location-first marketplace.
- The old provider page was not a true live pre-booking map.
- Tracking existed after assignment, but the new dispatch flow now requires fresh GPS for matching.
- The old Render configuration used multiple Gunicorn workers while Socket.IO was configured in-process; that is unsuitable for this threading-based pilot architecture.

### Communication
- Basic Socket.IO chat existed, but no price-negotiation workflow existed.
- Calling had no first-class booking action and users had no stored phone field.

### Marketplace scope
- The supplied code had a Delivery/Errands concept only after this upgrade; it now has a real service category and pickup/drop fields.
- The Student/Part-time panel in the supplied landing page is primarily a front-end mode/marketing panel; it is not a complete student opportunity marketplace with schedules, earnings, payouts and student-specific matching.

## What V2 adds

- Booking state machine
- Multi-provider broadcast offers
- 25-second offer expiry
- Automatic reassignment and radius expansion
- Atomic first-accept assignment
- Fresh-GPS eligibility
- Upfront service quote
- Live searching screen
- Live pre-booking provider map
- Driving ETA via OSRM
- Price negotiation
- Phone actions when a number is available
- Delivery & Errands
- Dispatch/event audit tables
- Socket.IO notifications for provider offers
- Single-worker deployment configuration for the current Socket.IO + SQLite pilot
- Gemini fallback corrected to `gemini-3.6-flash` primary and `gemini-2.5-flash` fallback

## What should still be built before a serious public launch

1. PostgreSQL + Redis instead of SQLite/in-process dispatch.
2. Push notifications (FCM/APNs) so providers receive requests while the browser/app is backgrounded.
3. Masked calling through a telephony provider rather than exposing phone numbers.
4. Proper KYC/background verification and document expiry.
5. Provider service areas, schedules, vacations and capacity.
6. Cancellation/no-show policy and customer/provider compensation rules.
7. Refunds, partial payments, invoices and tax/GST handling.
8. Fraud/risk controls, rate limiting, CSRF protection and audit logging.
9. Address search/geocoding and saved locations.
10. Rural/offline workflows: assisted booking, local-language UI, low-bandwidth mode and community agents.
11. Full student marketplace: availability calendar, student verification, earnings ledger and payout workflow.
12. Delivery fleet/runner workflow if SmartServe Express becomes a serious vertical.

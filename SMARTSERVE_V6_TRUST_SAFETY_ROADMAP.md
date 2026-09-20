# SmartServe V6 – Service Safety & Trust Upgrade

This release keeps the V5 customer-led provider selection architecture and adds the next operational layer.

## End-to-end service flow

1. Customer creates a service request and PIN.
2. SmartServe lists online, approved, skill-matched providers in the PIN service area.
3. Customer compares profile, trust score, rating, experience, completed jobs and distance.
4. Customer selects one provider.
5. Only that provider receives the request.
6. Provider accepts or declines.
7. If accepted, customer and provider can chat, call (when a phone number is available), negotiate price and track each other.
8. Provider reaches the location and presses **I've arrived**.
9. Customer receives a private 6-digit confirmation code and tells it to the provider after visually confirming arrival.
10. Provider enters the code; only then does the service move to `IN_PROGRESS`.
11. Provider uploads completion proof photos.
12. Customer sees the proof gallery and verifies the work.
13. Payment completes.
14. Customer submits a review.
15. The provider sees the review in the provider dashboard and public review page.

## Safety / trust rules

- Same-PIN providers are treated as the same local service area in PIN test mode.
- Offline providers are not shown as "available now".
- eKYC documents remain private; eKYC is not exposed as a public customer document.
- Completion proof is explicitly shown to the customer because it is part of service verification.
- Confirmation code is generated only after provider acceptance and is never displayed in the provider UI.
- Provider cannot start service directly from `ACCEPTED`; arrival + customer code are required.
- Call controls are visible to both parties. If a phone number is missing, the UI explains that the user must add one rather than silently hiding the call feature.
- Review records are linked to a completed service request and shown to the provider.

## Additional features recommended for the next major release

### 1. KYC trust badges
Separate:
- phone verified
- identity verified
- address/service-area verified
- skill certificate verified

Use these as transparent trust signals rather than one opaque trust number.

### 2. Provider portfolio
Allow providers to publish before/after work photos, certificates and service specialties.

### 3. Favorites / trusted providers
Customers should be able to save a provider and see them first on repeat bookings.

### 4. Scheduled bookings
Add future date/time bookings in addition to "book now".

### 5. Emergency / SOS
For eligible categories, provide emergency contact, trip sharing and a one-tap safety action.

### 6. Disputes and refunds
Create a structured post-service dispute workflow with proof, timestamps and admin review.

### 7. Smart service warranty
Let providers offer a configurable warranty period for selected services.

### 8. Repeat / AMC services
Useful for appliance maintenance, AC service, plumbing maintenance, cleaning and business customers.

### 9. Local language + low-bandwidth mode
Add Kannada/Hindi/Tamil/etc. UI and a lightweight mode for weaker town/rural connections.

### 10. Admin operations center
Admins should see live requests, provider availability, cancellations, disputes, KYC status, payments and suspicious activity.

### 11. Provider earnings
Add daily/weekly/monthly earnings, completed-job history, payout status and platform fees.

### 12. Customer trust center
Show verified-provider badges, completed-job count, review authenticity, arrival confirmation and payment protection in one place.

### 13. Fraud / abuse controls
Rate limits, duplicate-account detection, repeated cancellation scoring, suspicious payment patterns and review abuse detection.

### 14. Notifications
In-app + email + optional SMS/WhatsApp notifications for selection, acceptance, arrival, verification, payment and review events.

## Production architecture recommendation

For a larger deployment, move SQLite dispatch/state storage to PostgreSQL and use Redis for Socket.IO presence/events and background workers. Keep the current state machine and transactional guards; changing storage should not change the customer/provider experience.

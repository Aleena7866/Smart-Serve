# SmartServe V4 — Customer-Led Provider Choice

## Core architecture change

SmartServe no longer broadcasts a new customer request to providers for accept/reject.

### New flow

1. Customer creates a service request and enters a PIN code.
2. SmartServe finds approved, online providers who match the service and are available in that PIN/service area.
3. Customer sees the **live available-provider count**.
4. Customer sees a comparison card for every eligible provider, including:
   - trust score
   - rating and review count
   - experience
   - completed jobs
   - profile/bio
   - profile picture when available
   - masked mobile number when supplied
   - distance/ETA when coordinates are available
   - same-PIN indicator for local PIN test mode
5. Customer opens a full provider profile and selects one provider.
6. Only after selection is the provider assigned and notified.
7. The booking immediately becomes confirmed (`ACCEPTED`).
8. Customer and provider can then use realtime chat, calling and two-sided price negotiation.

## Availability definition

A provider is visible when all of the following are true:
- provider is approved;
- account role is provider;
- provider is online;
- provider is not currently handling another active job;
- provider's skills match the requested service;
- provider has a usable location, or shares the customer's PIN in local PIN test mode;
- GPS locations are fresh enough for distance matching.

The visible count is therefore an **available supply count**, not a count of all registered providers.

## Trust score

The displayed trust score is deliberately explainable for the prototype:
- 50% rating quality
- 20% experience (capped at 10 years)
- 15% profile completeness
- 15% completed jobs (capped at 50)

The score is used to rank the customer-choice list, but customers can still see all eligible providers and make their own decision.

## Realtime communication retained

- private Socket.IO request rooms
- two-sided chat
- database-backed message history
- right-side chat drawer
- call button using `tel:` when a phone number exists
- two-sided price proposals/counters
- acceptance locks the final amount for both parties

For production, replace `tel:` with a masked calling/relay provider.

## PIN testing

For local testing, use the same six-digit PIN on customer and provider accounts. The PIN is treated as the same service area even if geocoded coordinates differ slightly. Browser GPS remains the preferred production mechanism.

## Important migration behavior

The existing `match_offers` table is retained for database compatibility, but V4 does not create provider offers for new customer requests. The provider accept/reject API is disabled and returns HTTP 410.

The old direct selection URL is retained only as a compatibility redirect to the provider comparison page.

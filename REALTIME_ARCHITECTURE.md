# SmartServe V2 real-time architecture

## Booking state machine

`PENDING -> SEARCHING -> OFFERED -> ACCEPTED -> IN_PROGRESS -> AWAITING_VERIFICATION -> AWAITING_PAYMENT -> COMPLETED`

Terminal alternatives: `CANCELLED`, `EXPIRED`, `REJECTED`.

The customer does not manually select a single provider in the normal flow. SmartServe broadcasts short-lived offers to eligible providers and atomically assigns the first valid acceptance.

## Matching eligibility

A provider must:
- be approved;
- be online;
- have a browser GPS location;
- have a location updated recently;
- match the requested service;
- not already have an active job.

Candidates are ranked primarily by distance, with rating used as a tie-breaker.

## Location

Browser `navigator.geolocation.watchPosition()` sends location updates to `/api/location/update`.

- live location threshold: 45 seconds for dispatch;
- tracking UI hides stale coordinates rather than inventing a position;
- Leaflet renders the map;
- OSRM provides driving route ETA.

## Dispatch waves

Default:
- up to 5 providers per wave;
- 25-second offer lifetime;
- if no acceptance, expired offers are reassigned;
- search radius expands progressively up to 100 km.

## Communication

Socket.IO rooms are scoped to a booking. Chat and price proposals are emitted only to booking participants.

## Scaling path

For real production:
- PostgreSQL for transactional state;
- Redis for presence, Socket.IO message queue and distributed locks;
- background worker queue for dispatch/notifications;
- managed geocoding/routing service;
- object storage for photos;
- masked telephony provider for private calling;
- push notifications (FCM/APNs) for background provider alerts.

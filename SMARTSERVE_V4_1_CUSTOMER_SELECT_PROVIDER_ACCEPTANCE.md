# SmartServe V4.1 — Customer Selects, Provider Accepts

Flow: Customer creates request -> SmartServe shows all eligible online providers in the same 6-digit PIN service area -> customer compares trust score/profile/rating/experience/distance -> customer selects one -> request becomes ASSIGNED -> only that provider receives the request -> provider accepts or declines -> ACCEPTED enables chat/call/negotiation/tracking.

If provider declines, provider_id is cleared, the provider is recorded in service_events as PROVIDER_REJECTED, and the request returns to SEARCHING so the customer can choose another provider. Rejected providers are excluded from the same request's directory.

Same-PIN local test mode treats an exact customer/provider PIN match as the service-area match and does not require fresh GPS for discovery. Providers must still be approved, online, have the requested service skill, and not have another active assignment.

# SmartServe V7 release

This release builds on V5 customer-choice stability and V6 trust/safety. It adds the complete live-service lifecycle plus the 14 requested marketplace features. Existing databases are migrated additively; do not delete `smartserve.db` unless intentionally resetting test data.

Key invariant: customer selection creates `ASSIGNED`; provider acceptance is required before `ACCEPTED`.

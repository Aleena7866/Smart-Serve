# SmartServe V5 — Stability & Flow Fixes

## Critical fixes

1. **Provider profile schema migration**
   - Adds `users.profile_photo_path`.
   - Adds `providers.bio`, `providers.ekyc_document_path`, and `providers.ekyc_status`.
   - Runs automatically on existing and new SQLite databases.
   - Fixes the `/api/nearby-providers` crash: `sqlite3.OperationalError: no such column: p.bio`.

2. **Customer-choice state machine**
   - Customer discovers online, approved, service-matching providers in the same PIN area.
   - Customer selects one provider.
   - Selection creates `ASSIGNED`, not `ACCEPTED`.
   - Only the selected provider receives the request.
   - Provider explicitly accepts or declines.
   - Accept -> `ACCEPTED`; decline -> request returns to `SEARCHING` and the declined provider is excluded for that request.

3. **Registration stability**
   - Uses safe `request.form.get()` access.
   - Optional SMTP failure can never make a successfully committed account look like a failed registration.
   - Server logs the actual registration exception instead of hiding the database error.

4. **Graceful discovery errors**
   - Nearby-provider API returns JSON diagnostics instead of an HTML Flask 500 page.
   - `/api/system/diagnostic` verifies required schema columns for the logged-in user.

## Local demo

After installing dependencies and before testing customer choice:

```bash
python3 demo_setup.py
```

This sets the demo customer and Arjun Plumbing to PIN `585401`, with Arjun online.

## Expected test

1. Login customer `customer@smartserve.demo`.
2. Login provider `arjun.plumbing@smartserve.demo` in another browser.
3. Provider is already online after `demo_setup.py`.
4. Customer creates Plumbing request with PIN `585401`.
5. Customer sees `1 available now` (or more if other matching providers are online).
6. Customer opens profile and clicks **Select provider**.
7. Customer sees **Request sent — waiting for provider acceptance**.
8. Provider sees **Customer selected you** with **Accept service / Decline**.
9. Provider accepts.
10. Both sides get the accepted booking and realtime chat/call/negotiation controls.

The same logic works for newly registered provider accounts after they set their PIN and go online.

# SmartServe V7.2.1 Hotfix

Fixed startup crash:

`AssertionError: View function mapping is overwriting an existing endpoint function: system_diagnostic`

Cause: `/api/system/diagnostic` was registered twice in `app.py`.

The duplicate registration was removed. The first diagnostic endpoint is retained because it reports schema status, database path, journal mode, and busy timeout.

No database reset is required.

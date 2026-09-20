# SmartServe demo accounts

These accounts are seeded automatically when the database is initialized. They are for local/demo testing only.

## Customer
- Email: `customer@smartserve.demo`
- Password: `SmartServe@123`

## Provider accounts
All provider accounts use password `SmartServe@123`.

- `arjun.plumbing@smartserve.demo` — Plumbing
- `meera.electrical@smartserve.demo` — Electrical
- `ravi.carpentry@smartserve.demo` — Carpentry
- `neha.cleaning@smartserve.demo` — Cleaning
- `vikram.appliance@smartserve.demo` — Appliance Repair
- `sana.mobile@smartserve.demo` — Mobile Repair
- `kiran.laptop@smartserve.demo` — Laptop Repair
- `dev.it@smartserve.demo` — Basic IT Support

Run `python3 demo_setup.py` for a repeatable local demo: it places the demo customer and Arjun Plumbing in PIN 585401 and sets Arjun online. The helper does not delete existing data. Replace demo coordinates with real provider locations in production.

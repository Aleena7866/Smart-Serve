"""Prepare a local SmartServe demo without deleting existing accounts.

Usage:
    python3 demo_setup.py

It puts the seeded demo customer and Plumbing provider in the same PIN area
and makes the Plumbing provider online. This is for local demonstration only.
"""
import os
from database import get_db_connection, init_database

PIN = "585401"
LAT, LON = 17.9136, 77.5199

init_database()
conn = get_db_connection()
try:
    customer = conn.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(?)", ("customer@smartserve.demo",)).fetchone()
    provider_user = conn.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(?)", ("arjun.plumbing@smartserve.demo",)).fetchone()
    if not customer or not provider_user:
        raise SystemExit("Demo accounts were not found; run app.py once to seed them.")
    conn.execute("""UPDATE users SET pincode=?,latitude=?,longitude=?,location_accuracy=1000,
                    location_updated_at=CURRENT_TIMESTAMP,location_source='PINCODE' WHERE id=?""",
                 (PIN, LAT, LON, customer["id"]))
    conn.execute("""UPDATE users SET pincode=?,latitude=?,longitude=?,location_accuracy=1000,
                    location_updated_at=CURRENT_TIMESTAMP,location_source='PINCODE',
                    is_online=1,last_seen_at=CURRENT_TIMESTAMP WHERE id=?""",
                 (PIN, LAT, LON, provider_user["id"]))
    conn.commit()
    print("SmartServe demo ready: customer + Arjun Plumbing provider are in PIN 585401.")
    print("Provider is ONLINE. Login with the credentials in DEMO_ACCOUNTS.md.")
finally:
    conn.close()

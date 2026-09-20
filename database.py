from werkzeug.security import generate_password_hash
import sqlite3
import os
import time


DATABASE = os.getenv("SMARTSERVE_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "smartserve.db"
)


def _is_locked_error(exc):
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


class SmartServeConnection(sqlite3.Connection):
    """SQLite connection with short lock retries for realtime multi-user use."""
    def _retry(self, fn, *args, **kwargs):
        last = None
        for attempt in range(8):
            try:
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                last = exc
                if not _is_locked_error(exc) or attempt == 7:
                    raise
                time.sleep(0.10 * (attempt + 1))
        raise last

    def execute(self, *args, **kwargs):
        return self._retry(super().execute, *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._retry(super().executemany, *args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self._retry(super().executescript, *args, **kwargs)

    def commit(self):
        return self._retry(super().commit)


DATABASE_TIMEOUT_SECONDS = 30


# =========================================================
# DATABASE CONNECTION
# =========================================================

def get_db_connection():
    connection = sqlite3.connect(
        DATABASE,
        timeout=DATABASE_TIMEOUT_SECONDS,
        check_same_thread=False,
        factory=SmartServeConnection,
    )
    connection.row_factory = sqlite3.Row
    # WAL lets readers continue while a short writer transaction is active.
    # busy_timeout prevents transient realtime requests from failing immediately.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


# =========================================================
# ADD COLUMN ONLY IF IT DOES NOT ALREADY EXIST
# =========================================================

def add_column_if_missing(
    connection,
    table_name,
    column_name,
    column_definition
):

    cursor = connection.cursor()

    columns = cursor.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    existing_columns = [
        column["name"]
        for column in columns
    ]

    if column_name not in existing_columns:

        cursor.execute(
            f"""
            ALTER TABLE {table_name}
            ADD COLUMN {column_name} {column_definition}
            """
        )

        print(
            f"Added column: "
            f"{table_name}.{column_name}"
        )


# =========================================================
# INITIALIZE DATABASE
# =========================================================

def init_database():

    connection = get_db_connection()

    cursor = connection.cursor()


    # =====================================================
    # USERS
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            name TEXT NOT NULL,

            email TEXT UNIQUE NOT NULL,

            password TEXT NOT NULL,

            role TEXT NOT NULL

        )
    """)


    # =====================================================
    # REAL-TIME USER LOCATION
    # =====================================================

    add_column_if_missing(
        connection,
        "users",
        "latitude",
        "REAL"
    )

    add_column_if_missing(
        connection,
        "users",
        "longitude",
        "REAL"
    )

    add_column_if_missing(
        connection,
        "users",
        "location_updated_at",
        "TIMESTAMP"
    )



    # =====================================================
    # GOOGLE OAUTH / REAL-TIME PRESENCE
    # =====================================================
    add_column_if_missing(connection, "users", "google_sub", "TEXT")
    add_column_if_missing(connection, "users", "auth_provider", "TEXT DEFAULT 'local'")
    add_column_if_missing(connection, "users", "email_verified", "INTEGER DEFAULT 0")
    add_column_if_missing(connection, "users", "is_online", "INTEGER DEFAULT 0")
    add_column_if_missing(connection, "users", "last_seen_at", "TIMESTAMP")


    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub ON users(google_sub) WHERE google_sub IS NOT NULL")

    # =====================================================
    # SERVICES
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS services (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            name TEXT NOT NULL,

            description TEXT

        )
    """)


    # =====================================================
    # PROVIDERS
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS providers (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            user_id INTEGER NOT NULL,

            skills TEXT NOT NULL,

            experience INTEGER DEFAULT 0,

            rating REAL DEFAULT 0,

            approved INTEGER DEFAULT 1,

            FOREIGN KEY (user_id)
                REFERENCES users(id)

        )
    """)


    # =====================================================
    # PROVIDER SERVICE OFFERINGS
    # =====================================================
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS provider_services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(provider_id, service_id),
            FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE,
            FOREIGN KEY(service_id) REFERENCES services(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_provider_services_provider ON provider_services(provider_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_provider_services_service ON provider_services(service_id)")

    # =====================================================
    # SERVICE REQUESTS
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_requests (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            customer_id INTEGER NOT NULL,

            service_id INTEGER NOT NULL,

            description TEXT NOT NULL,

            image_path TEXT,

            ai_analysis TEXT,

            estimated_price TEXT,

            provider_id INTEGER,

            status TEXT DEFAULT 'PENDING',

            created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (customer_id)
                REFERENCES users(id),

            FOREIGN KEY (service_id)
                REFERENCES services(id),

            FOREIGN KEY (provider_id)
                REFERENCES providers(id)

        )
    """)


    # =====================================================
    # AI ANALYSIS COLUMNS
    # =====================================================
    #
    # These columns store the complete AI analysis.
    #
    # ai_analysis
    #     Legacy column.
    #
    # ai_problem
    #     Actual problem identified by AI.
    #
    # ai_possible_cause
    #     Possible reason/cause.
    #
    # ai_difficulty
    #     Difficulty level.
    #
    # ai_recommended_service
    #     Recommended service/action.
    #
    # ai_safety_note
    #     Important safety information.
    #
    # estimated_price
    #     Estimated service price.
    #
    # =====================================================


    add_column_if_missing(
        connection,
        "service_requests",
        "ai_problem",
        "TEXT"
    )


    add_column_if_missing(
        connection,
        "service_requests",
        "ai_possible_cause",
        "TEXT"
    )


    add_column_if_missing(
        connection,
        "service_requests",
        "ai_difficulty",
        "TEXT"
    )


    add_column_if_missing(
        connection,
        "service_requests",
        "ai_recommended_service",
        "TEXT"
    )


    add_column_if_missing(
        connection,
        "service_requests",
        "ai_safety_note",
        "TEXT"
    )


    # =====================================================
    # PAYMENTS / COMPLETION VERIFICATION
    # =====================================================

    add_column_if_missing(connection, "service_requests", "agreed_amount", "REAL")
    add_column_if_missing(connection, "service_requests", "payment_status", "TEXT DEFAULT 'UNPAID'")
    add_column_if_missing(connection, "service_requests", "razorpay_order_id", "TEXT")
    add_column_if_missing(connection, "service_requests", "razorpay_payment_id", "TEXT")
    add_column_if_missing(connection, "service_requests", "paid_at", "TIMESTAMP")
    add_column_if_missing(connection, "service_requests", "completion_proof_paths", "TEXT")
    add_column_if_missing(connection, "service_requests", "customer_verification_path", "TEXT")
    add_column_if_missing(connection, "service_requests", "verified_at", "TIMESTAMP")
    # =====================================================
    # ARRIVAL / CUSTOMER CONFIRMATION CODE
    # =====================================================
    add_column_if_missing(connection, "service_requests", "confirmation_code", "TEXT")
    add_column_if_missing(connection, "service_requests", "arrival_status", "TEXT DEFAULT 'NOT_STARTED'")
    add_column_if_missing(connection, "service_requests", "arrived_at", "TIMESTAMP")
    add_column_if_missing(connection, "service_requests", "service_started_at", "TIMESTAMP")
    add_column_if_missing(connection, "service_requests", "confirmation_verified_at", "TIMESTAMP")
    add_column_if_missing(connection, "service_requests", "provider_arrival_note", "TEXT")



    # =====================================================
    # MESSAGES / CHAT
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            request_id INTEGER NOT NULL,

            sender_id INTEGER NOT NULL,

            message TEXT NOT NULL,

            created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (request_id)
                REFERENCES service_requests(id),

            FOREIGN KEY (sender_id)
                REFERENCES users(id)

        )
    """)


    # =====================================================
    # REVIEWS
    # =====================================================

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reviews (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            request_id INTEGER NOT NULL,

            customer_id INTEGER NOT NULL,

            provider_id INTEGER NOT NULL,

            rating INTEGER NOT NULL,

            review TEXT,

            created_at TIMESTAMP
                DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (request_id)
                REFERENCES service_requests(id),

            FOREIGN KEY (customer_id)
                REFERENCES users(id),

            FOREIGN KEY (provider_id)
                REFERENCES providers(id)

        )
    """)


    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reviews_provider_created ON reviews(provider_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_request_created ON messages(request_id, created_at)")

    # =====================================================
    # DEFAULT SERVICES
    # =====================================================

    services = [

        (
            "Plumbing",
            "Pipe, tap and water related services"
        ),

        (
            "Electrical",
            "Electrical repair and installation"
        ),

        (
            "Carpentry",
            "Furniture and wood related services"
        ),

        (
            "Appliance Repair",
            "Repair home appliances"
        ),

        (
            "Cleaning",
            "Home and office cleaning"
        ),

        (
            "Mobile Repair",
            "Mobile phone repair"
        ),

        (
            "Laptop Repair",
            "Laptop and computer repair"
        ),

        (
            "Basic IT Support",
            "Basic computer and software support"
        ),
        (
            "Delivery & Errands",
            "Local pickup, drop-off and anything-from-nearby-store delivery"
        )

    ]


    # =====================================================
    # INSERT DEFAULT SERVICES
    # =====================================================
    #
    # Do NOT blindly insert the services again.
    #
    # We check whether the service already exists.
    # This helps prevent new duplicate services.
    #
    # =====================================================

    for service_name, service_description in services:

        cursor.execute("""
            INSERT INTO services
            (
                name,
                description
            )

            SELECT ?, ?

            WHERE NOT EXISTS (

                SELECT 1

                FROM services

                WHERE LOWER(name) = LOWER(?)

            )
        """, (
            service_name,
            service_description,
            service_name
        ))


    # Keep the normalized provider service catalog in sync for existing accounts
    # created by older SmartServe versions that only stored comma-separated skills.
    for provider_row in cursor.execute("SELECT id,skills FROM providers").fetchall():
        pid=int(provider_row["id"])
        existing_map=cursor.execute("SELECT 1 FROM provider_services WHERE provider_id=? LIMIT 1", (pid,)).fetchone()
        if not existing_map and provider_row["skills"]:
            for token in str(provider_row["skills"]).replace(';', ',').replace('|', ',').split(','):
                token=token.strip()
                if not token: continue
                sr=cursor.execute("SELECT id FROM services WHERE LOWER(name)=LOWER(?)", (token,)).fetchone()
                if sr: cursor.execute("INSERT OR IGNORE INTO provider_services(provider_id,service_id) VALUES(?,?)", (pid,sr["id"]))


    # =====================================================
    # DEMO DATA FOR FULL END-TO-END TESTING
    # =====================================================
    demo_password = generate_password_hash("SmartServe@123")
    demo_customer = cursor.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(?)", ("customer@smartserve.demo",)).fetchone()
    if not demo_customer:
        cursor.execute("""INSERT INTO users(name,email,password,role,auth_provider,email_verified,is_online,latitude,longitude) VALUES(?,?,?,?,?,?,?,?,?)""", ("Demo Customer","customer@smartserve.demo",demo_password,"customer","email",1,0,None,None))
    else:
        # Clear legacy seeded coordinates once. A real browser update sets
        # location_updated_at, after which the user's real location is preserved.
        cursor.execute("UPDATE users SET is_online=0, latitude=NULL, longitude=NULL WHERE id=? AND location_updated_at IS NULL", (demo_customer["id"],))
    provider_seed = [
        ("Arjun Plumbing","arjun.plumbing@smartserve.demo","Plumbing,Pipe Repair,Tap Repair",7,4.9),
        ("Meera Electrical","meera.electrical@smartserve.demo","Electrical,Wiring,Installation",8,4.8),
        ("Ravi Carpentry","ravi.carpentry@smartserve.demo","Carpentry,Furniture,Wood Repair",9,4.8),
        ("Neha Cleaning","neha.cleaning@smartserve.demo","Cleaning,Deep Cleaning,Home Cleaning",6,4.7),
        ("Vikram Appliance","vikram.appliance@smartserve.demo","Appliance Repair,AC Repair,Refrigerator Repair",10,4.8),
        ("Sana Mobile","sana.mobile@smartserve.demo","Mobile Repair,Screen Repair,Battery Repair",5,4.7),
        ("Kiran Laptop","kiran.laptop@smartserve.demo","Laptop Repair,Computer Repair,Hardware",6,4.8),
        ("Dev IT Support","dev.it@smartserve.demo","Basic IT Support,Software,WiFi Support",5,4.6),
    ]
    for name,email,skills,experience,rating in provider_seed:
        existing = cursor.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(?)", (email,)).fetchone()
        if existing:
            uid=existing["id"]
            # Never overwrite a location that came from a real browser GPS update.
            cursor.execute("UPDATE users SET is_online=CASE WHEN location_updated_at IS NULL THEN 0 ELSE is_online END, latitude=CASE WHEN location_updated_at IS NULL THEN NULL ELSE latitude END, longitude=CASE WHEN location_updated_at IS NULL THEN NULL ELSE longitude END WHERE id=?", (uid,))
        else:
            cursor.execute("""INSERT INTO users(name,email,password,role,auth_provider,email_verified,is_online,latitude,longitude,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""", (name,email,demo_password,"provider","email",1,0,None,None))
            uid=cursor.lastrowid
        existing_provider=cursor.execute("SELECT id FROM providers WHERE user_id=?", (uid,)).fetchone()
        if existing_provider:
            cursor.execute("UPDATE providers SET skills=?, experience=?, rating=?, approved=1 WHERE id=?", (skills,experience,rating,existing_provider["id"]))
        else:
            cursor.execute("INSERT INTO providers(user_id,skills,experience,rating,approved) VALUES(?,?,?,?,1)", (uid,skills,experience,rating))
        pid_row = cursor.execute("SELECT id FROM providers WHERE user_id=?", (uid,)).fetchone()
        if pid_row:
            pid = int(pid_row["id"])
            cursor.execute("DELETE FROM provider_services WHERE provider_id=?", (pid,))
            for service_token in [x.strip() for x in skills.split(',') if x.strip()]:
                sr = cursor.execute("SELECT id FROM services WHERE LOWER(name)=LOWER(?)", (service_token,)).fetchone()
                if sr:
                    cursor.execute("INSERT OR IGNORE INTO provider_services(provider_id,service_id) VALUES(?,?)", (pid, sr["id"]))


    # =====================================================
    # SMARTSERVE V2 BOOKING / DISPATCH / NEGOTIATION
    # =====================================================
    # These migrations are additive so existing demo databases keep working.
    v2_columns = [
        ("users", "phone", "TEXT"),
        ("users", "location_accuracy", "REAL"),
        ("users", "pincode", "TEXT"),
        ("users", "location_source", "TEXT"),
        ("service_requests", "customer_latitude", "REAL"),
        ("service_requests", "customer_longitude", "REAL"),
        ("service_requests", "customer_location_accuracy", "REAL"),
        ("service_requests", "customer_pincode", "TEXT"),
        ("service_requests", "customer_location_source", "TEXT"),
        ("service_requests", "service_radius_km", "REAL DEFAULT 10"),
        ("service_requests", "address_text", "TEXT"),
        ("service_requests", "preferred_time", "TEXT"),
        ("service_requests", "booking_mode", "TEXT DEFAULT 'ON_DEMAND'"),
        ("service_requests", "search_started_at", "TIMESTAMP"),
        ("service_requests", "search_deadline", "TIMESTAMP"),
        ("service_requests", "matching_wave", "INTEGER DEFAULT 0"),
        ("service_requests", "upfront_min", "REAL"),
        ("service_requests", "upfront_max", "REAL"),
        ("service_requests", "travel_fee", "REAL DEFAULT 0"),
        ("service_requests", "platform_fee", "REAL DEFAULT 0"),
        ("service_requests", "final_amount", "REAL"),
        ("service_requests", "accepted_at", "TIMESTAMP"),
        ("service_requests", "started_at", "TIMESTAMP"),
        ("service_requests", "completed_at", "TIMESTAMP"),
        ("service_requests", "cancelled_at", "TIMESTAMP"),
        ("service_requests", "cancellation_reason", "TEXT"),
        ("service_requests", "delivery_type", "TEXT"),
        ("service_requests", "pickup_address", "TEXT"),
        ("service_requests", "drop_address", "TEXT"),
        ("service_requests", "delivery_notes", "TEXT"),
        ("service_requests", "negotiation_status", "TEXT DEFAULT 'NONE'"),
    ]
    for table_name, column_name, column_definition in v2_columns:
        add_column_if_missing(connection, table_name, column_name, column_definition)

    # V4 provider/customer profile fields. These migrations are required on
    # both brand-new and existing SmartServe databases.
    profile_columns = [
        ("users", "profile_photo_path", "TEXT"),
        ("providers", "bio", "TEXT"),
        ("providers", "ekyc_document_path", "TEXT"),
        ("providers", "ekyc_status", "TEXT DEFAULT 'NOT_SUBMITTED'"),
    ]
    for table_name, column_name, column_definition in profile_columns:
        add_column_if_missing(connection, table_name, column_name, column_definition)
    cursor.execute("UPDATE providers SET ekyc_status='NOT_SUBMITTED' WHERE ekyc_status IS NULL OR TRIM(ekyc_status)=''")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS match_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            provider_id INTEGER NOT NULL,
            wave INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'OFFERED',
            offered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            responded_at TIMESTAMP,
            distance_km REAL,
            eta_minutes INTEGER,
            quoted_amount REAL,
            FOREIGN KEY(request_id) REFERENCES service_requests(id),
            FOREIGN KEY(provider_id) REFERENCES providers(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_match_offers_request_status ON match_offers(request_id,status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_match_offers_provider_status ON match_offers(provider_id,status)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            actor_user_id INTEGER,
            event_type TEXT NOT NULL,
            message TEXT,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(request_id) REFERENCES service_requests(id),
            FOREIGN KEY(actor_user_id) REFERENCES users(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_service_events_request ON service_events(request_id,created_at)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_negotiations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            sender_user_id INTEGER NOT NULL,
            proposed_amount REAL NOT NULL,
            message TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            responded_at TIMESTAMP,
            FOREIGN KEY(request_id) REFERENCES service_requests(id),
            FOREIGN KEY(sender_user_id) REFERENCES users(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_negotiations_request ON price_negotiations(request_id,created_at)")

    # Base pricing is intentionally conservative and can be changed later
    # without changing application code.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_pricing (
            service_id INTEGER PRIMARY KEY,
            base_price REAL NOT NULL DEFAULT 299,
            min_price REAL NOT NULL DEFAULT 199,
            max_price REAL NOT NULL DEFAULT 1499,
            included_km REAL NOT NULL DEFAULT 3,
            per_km REAL NOT NULL DEFAULT 12,
            platform_fee REAL NOT NULL DEFAULT 20,
            FOREIGN KEY(service_id) REFERENCES services(id)
        )
    """)
    pricing_defaults = {
        "Plumbing": (299,199,1499,3,12,20),
        "Electrical": (299,199,1499,3,12,20),
        "Carpentry": (349,249,1999,3,12,20),
        "Cleaning": (399,299,2499,3,10,20),
        "Appliance Repair": (399,299,2499,3,12,25),
        "Mobile Repair": (299,199,2999,3,10,20),
        "Laptop Repair": (399,299,3999,3,10,25),
        "Basic IT Support": (299,199,1999,3,10,20),
        "Delivery & Errands": (149,99,1999,2,15,15),
    }
    for service_name, values in pricing_defaults.items():
        service_row = cursor.execute("SELECT id FROM services WHERE LOWER(name)=LOWER(?) LIMIT 1", (service_name,)).fetchone()
        if service_row:
            cursor.execute("""
                INSERT OR IGNORE INTO service_pricing
                (service_id,base_price,min_price,max_price,included_km,per_km,platform_fee)
                VALUES(?,?,?,?,?,?,?)
            """, (service_row["id"], *values))

    # =====================================================
    # V7 TRUST / MARKETPLACE / OPERATIONS FEATURES
    # =====================================================
    v7_columns = [
        ("users", "preferred_language", "TEXT DEFAULT 'en'"),
        ("users", "low_bandwidth_mode", "INTEGER DEFAULT 0"),
        ("users", "trusted_contact_name", "TEXT"),
        ("users", "trusted_contact_phone", "TEXT"),
        ("service_requests", "scheduled_at", "TIMESTAMP"),
        ("service_requests", "warranty_days", "INTEGER DEFAULT 0"),
        ("service_requests", "warranty_until", "TIMESTAMP"),
        ("service_requests", "arrival_confirmed_by_customer", "INTEGER DEFAULT 0"),
        ("service_requests", "route_status", "TEXT DEFAULT 'NOT_STARTED'"),
    ]
    for table_name, column_name, column_definition in v7_columns:
        add_column_if_missing(connection, table_name, column_name, column_definition)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS provider_badges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            badge_key TEXT NOT NULL,
            label TEXT NOT NULL,
            awarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(provider_id,badge_key),
            FOREIGN KEY(provider_id) REFERENCES providers(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            image_path TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(provider_id) REFERENCES providers(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS favorite_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            provider_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(customer_id,provider_id),
            FOREIGN KEY(customer_id) REFERENCES users(id),
            FOREIGN KEY(provider_id) REFERENCES providers(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_warranties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL UNIQUE,
            provider_id INTEGER NOT NULL,
            warranty_days INTEGER NOT NULL DEFAULT 7,
            warranty_until TIMESTAMP,
            terms TEXT,
            status TEXT DEFAULT 'ACTIVE',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(request_id) REFERENCES service_requests(id),
            FOREIGN KEY(provider_id) REFERENCES providers(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS disputes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            opened_by INTEGER NOT NULL,
            reason TEXT NOT NULL,
            description TEXT,
            evidence_paths TEXT,
            status TEXT DEFAULT 'OPEN',
            resolution TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(request_id) REFERENCES service_requests(id),
            FOREIGN KEY(opened_by) REFERENCES users(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recurring_bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            provider_id INTEGER,
            service_id INTEGER NOT NULL,
            frequency TEXT NOT NULL,
            next_run_at TIMESTAMP,
            active INTEGER DEFAULT 1,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(customer_id) REFERENCES users(id),
            FOREIGN KEY(provider_id) REFERENCES providers(id),
            FOREIGN KEY(service_id) REFERENCES services(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS provider_earnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            request_id INTEGER,
            gross_amount REAL NOT NULL DEFAULT 0,
            platform_fee REAL NOT NULL DEFAULT 0,
            net_amount REAL NOT NULL DEFAULT 0,
            payout_status TEXT DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(provider_id) REFERENCES providers(id),
            FOREIGN KEY(request_id) REFERENCES service_requests(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trusted_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            relationship TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id,phone),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fraud_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            request_id INTEGER,
            signal_type TEXT NOT NULL,
            severity TEXT DEFAULT 'LOW',
            details TEXT,
            status TEXT DEFAULT 'OPEN',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(request_id) REFERENCES service_requests(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            severity TEXT DEFAULT 'INFO',
            title TEXT NOT NULL,
            message TEXT,
            request_id INTEGER,
            user_id INTEGER,
            status TEXT DEFAULT 'OPEN',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(request_id) REFERENCES service_requests(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS provider_metrics (
            provider_id INTEGER PRIMARY KEY,
            accepted_jobs INTEGER DEFAULT 0,
            completed_jobs INTEGER DEFAULT 0,
            declined_jobs INTEGER DEFAULT 0,
            cancelled_jobs INTEGER DEFAULT 0,
            avg_response_seconds REAL DEFAULT 0,
            last_calculated_at TIMESTAMP,
            FOREIGN KEY(provider_id) REFERENCES providers(id)
        )
    """)
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_portfolio_provider ON portfolio_items(provider_id,created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_favorites_customer ON favorite_providers(customer_id)",
        "CREATE INDEX IF NOT EXISTS idx_disputes_status ON disputes(status,created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_earnings_provider ON provider_earnings(provider_id,created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_fraud_status ON fraud_signals(status,severity,created_at DESC)",
    ]:
        cursor.execute(idx)

    # Automatically award transparent verification badges. These are informational;
    # they never expose private KYC documents.
    cursor.execute("""
        INSERT OR IGNORE INTO provider_badges(provider_id,badge_key,label)
        SELECT p.id,'profile_complete','Profile complete'
        FROM providers p JOIN users u ON u.id=p.user_id
        WHERE COALESCE(TRIM(p.skills),'')<>'' AND COALESCE(p.experience,0)>0 AND COALESCE(TRIM(p.bio),'')<>''
    """)
    cursor.execute("""
        INSERT OR IGNORE INTO provider_badges(provider_id,badge_key,label)
        SELECT p.id,'phone_verified','Phone added'
        FROM providers p JOIN users u ON u.id=p.user_id
        WHERE COALESCE(TRIM(u.phone),'')<>''
    """)
    cursor.execute("""
        INSERT OR IGNORE INTO provider_badges(provider_id,badge_key,label)
        SELECT id,'identity_submitted','eKYC submitted'
        FROM providers WHERE ekyc_status='SUBMITTED'
    """)
    cursor.execute("""
        INSERT OR IGNORE INTO provider_badges(provider_id,badge_key,label)
        SELECT p.id,'experienced','Experienced provider'
        FROM providers p WHERE COALESCE(p.experience,0)>=5
    """)

    # Demo admin for local evaluation only.
    admin_exists = cursor.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(?)", ("admin@smartserve.demo",)).fetchone()
    if not admin_exists:
        cursor.execute("INSERT INTO users(name,email,password,role,auth_provider,email_verified,is_online) VALUES(?,?,?,?,?,?,?)", ("SmartServe Admin","admin@smartserve.demo",generate_password_hash("SmartServe@123"),"admin","email",1,1))

    # =====================================================
    # SAVE CHANGES
    # =====================================================

    connection.commit()

    connection.close()


# =========================================================
# RUN DATABASE INITIALIZATION
# =========================================================

if __name__ == "__main__":

    init_database()

    print(
        "Smart Serve database initialized successfully!"
    )

"""SmartServe provider matching utilities.

V4 uses customer-led provider choice: the discovery engine returns eligible,
currently available professionals for comparison. It does not notify providers
or create offers during discovery. Legacy wave/offer helpers remain only for
database compatibility with earlier SmartServe builds.

This is a single-process/SQLite implementation suitable for a pilot. For
national scale, move availability and dispatch state to PostgreSQL/Redis.
"""
import math
import sqlite3
import re
from datetime import datetime, timedelta

WAVE_SIZE = 5
OFFER_TTL_SECONDS = 25
LOCATION_FRESH_SECONDS = 45
MAX_RADIUS_KM = 100
PIN_TEST_MODE = True  # local demo: same Indian PIN is sufficient for matching

def utcnow():
    return datetime.utcnow()

def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z","").replace(" ","T"))
    except Exception:
        return None

def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    p = math.pi / 180
    dlat = (float(lat2)-float(lat1))*p
    dlon = (float(lon2)-float(lon1))*p
    a = math.sin(dlat/2)**2 + math.cos(float(lat1)*p)*math.cos(float(lat2)*p)*math.sin(dlon/2)**2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0,1-a)))

def fresh_location(updated_at, max_age=LOCATION_FRESH_SECONDS):
    dt = parse_dt(updated_at)
    return bool(dt and (utcnow()-dt).total_seconds() <= max_age)

def _active_provider_ids(conn):
    rows = conn.execute("""
        SELECT DISTINCT provider_id FROM service_requests
        WHERE provider_id IS NOT NULL
          AND status IN ('ASSIGNED','ACCEPTED','IN_PROGRESS','AWAITING_VERIFICATION','AWAITING_PAYMENT')
    """).fetchall()
    return {int(r["provider_id"]) for r in rows}

def candidates(conn, request_id, radius_km, limit=WAVE_SIZE, exclude_existing=True):
    req = conn.execute("""
        SELECT sr.*, s.name AS service_name
        FROM service_requests sr JOIN services s ON s.id=sr.service_id
        WHERE sr.id=?
    """,(request_id,)).fetchone()
    if not req or req["customer_latitude"] is None or req["customer_longitude"] is None:
        return []
    active = _active_provider_ids(conn)
    # A provider already offered this request is not offered again.
    existing = {int(r["provider_id"]) for r in conn.execute(
        "SELECT provider_id FROM match_offers WHERE request_id=? AND status IN ('OFFERED','ACCEPTED')",
        (request_id,)
    ).fetchall()} if exclude_existing else set()
    rows = conn.execute("""
        SELECT p.id AS provider_id,p.skills,p.experience,p.rating,p.approved,p.user_id,
               u.name,u.phone,u.latitude,u.longitude,u.location_updated_at,u.location_source,
               u.pincode,u.is_online,u.last_seen_at
        FROM providers p JOIN users u ON u.id=p.user_id
        WHERE p.approved=1 AND u.role='provider' AND u.is_online=1
    """).fetchall()
    out=[]
    for row in rows:
        pid=int(row["provider_id"])
        if pid in active or pid in existing:
            continue
        source=(row["location_source"] or "GPS").upper()
        same_pin = bool(PIN_TEST_MODE and str(row["pincode"] or "") == str(req["customer_pincode"] or "") and str(req["customer_pincode"] or "").isdigit() and len(str(req["customer_pincode"] or "")) == 6)
        if row["latitude"] is None or row["longitude"] is None:
            if not same_pin:
                continue
        # PINCODE is an explicit testing/service-area location and must not expire
        # after 45 seconds like browser GPS. GPS locations still require freshness.
        # PIN is an explicit local/test service-area location and is intentionally
        # persistent. Browser GPS remains freshness-gated.
        if not same_pin and source != 'PINCODE' and not fresh_location(row["location_updated_at"]):
            continue

        # Normalize provider skills robustly. A provider may have values such as
        # "Plumbing,Electrical,Carpentry,CleaningMobile Repair".
        service=(req["service_name"] or "").strip().lower()
        raw_skills=(row["skills"] or "").lower()
        import re
        skill_tokens={re.sub(r'\s+',' ',x.strip()) for x in re.split(r'[,;|]',raw_skills) if x.strip()}
        skill_match = service in skill_tokens or any(
            service == re.sub(r'\s+',' ',x.strip()) or service in re.sub(r'\s+',' ',x.strip())
            for x in skill_tokens
        )
        if not skill_match:
            continue

        d=0.0 if same_pin and (row["latitude"] is None or row["longitude"] is None) else haversine(req["customer_latitude"],req["customer_longitude"],row["latitude"],row["longitude"])
        # In local PIN test mode, identical PINs are deliberately treated as the
        # same service area even if an external geocoder gives slightly different
        # points for the same PIN. Production GPS matching remains distance-based.
        if d>radius_km and not same_pin:
            continue
        # Simple, explainable ranking: distance dominates, rating breaks ties.
        rating=float(row["rating"] or 0)
        score=d - min(rating,5)*0.03
        out.append((score,d,row))
    out.sort(key=lambda x:x[0])
    return [{"provider":dict(x[2]),"distance_km":round(x[1],2),"eta_minutes":max(1,round(x[1]/0.35))} for x in out[:limit]]



def _service_matches(skills, service_name):
    service=(service_name or '').strip().lower()
    raw=(skills or '').lower()
    tokens=[re.sub(r'\s+',' ',x.strip()) for x in re.split(r'[,;|]',raw) if x.strip()]
    return bool(service and any(service == token or service in token for token in tokens))

def _masked_phone(phone):
    if not phone:
        return None
    value=str(phone).strip()
    digits=''.join(ch for ch in value if ch.isdigit())
    if len(digits) >= 4:
        return ('+' + digits[:2] + '******' + digits[-2:]) if value.startswith('+') else ('******' + digits[-4:])
    return 'Available'

def available_providers(conn, request_id, radius_km=None, limit=50):
    """Return providers the customer can choose from before assignment.

    This is deliberately different from the old broadcast-offer candidate list: no
    match_offers are created and providers are never notified merely because they
    are visible here.
    """
    req=conn.execute("""SELECT sr.*,s.name AS service_name FROM service_requests sr
                       JOIN services s ON s.id=sr.service_id WHERE sr.id=?""",(request_id,)).fetchone()
    if not req:
        return []
    try: radius=float(radius_km or req['service_radius_km'] or 10)
    except (TypeError,ValueError): radius=10
    radius=max(1,min(radius,MAX_RADIUS_KM))
    active=_active_provider_ids(conn)
    mapped_service_providers={int(r['provider_id']) for r in conn.execute(
        'SELECT provider_id FROM provider_services WHERE service_id=?', (req['service_id'],)
    ).fetchall()}
    rejected={int(r['actor_user_id']) for r in conn.execute(
        "SELECT actor_user_id FROM service_events WHERE request_id=? AND event_type='PROVIDER_REJECTED' AND actor_user_id IS NOT NULL",
        (request_id,)
    ).fetchall()}
    rows=conn.execute("""
        SELECT p.id AS provider_id,p.skills,p.experience,p.rating,p.approved,p.user_id,p.bio,p.ekyc_status,
               u.name,u.phone,u.profile_photo_path,u.latitude,u.longitude,u.location_updated_at,u.location_source,
               u.pincode,u.is_online,u.last_seen_at,
               (SELECT COUNT(*) FROM reviews rv WHERE rv.provider_id=p.id) AS review_count,
               (SELECT COUNT(*) FROM service_requests sr2 WHERE sr2.provider_id=p.id AND sr2.status='COMPLETED') AS completed_jobs
        FROM providers p JOIN users u ON u.id=p.user_id
        WHERE p.approved=1 AND u.role='provider' AND u.is_online=1
    """).fetchall()
    out=[]
    customer_pin=str(req['customer_pincode'] or '').strip()
    customer_has_pin=len(customer_pin)==6 and customer_pin.isdigit()
    for row in rows:
        pid=int(row['provider_id'])
        skill_ok=_service_matches(row['skills'],req['service_name'])
        mapped_ok=pid in mapped_service_providers
        if pid in active or int(row['user_id']) in rejected or not (mapped_ok or skill_ok):
            continue
        pin=str(row['pincode'] or '').strip()
        same_pin=bool(PIN_TEST_MODE and customer_has_pin and pin==customer_pin)
        source=(row['location_source'] or 'GPS').upper()
        # When a customer supplied a PIN, the customer-choice directory is a
        # service-area directory: show every online eligible provider in that
        # exact PIN, even if the geocoder points differ slightly.
        if customer_has_pin and not same_pin:
            continue
        if not same_pin and (row['latitude'] is None or row['longitude'] is None):
            continue
        if not same_pin and source != 'PINCODE' and not fresh_location(row['location_updated_at']):
            continue
        if row['latitude'] is not None and row['longitude'] is not None and req['customer_latitude'] is not None and req['customer_longitude'] is not None:
            distance=haversine(req['customer_latitude'],req['customer_longitude'],row['latitude'],row['longitude'])
        else:
            distance=0.0 if same_pin else None
        if distance is not None and distance>radius and not same_pin:
            continue
        rating=min(5,max(0,float(row['rating'] or 0)))
        experience=min(10,max(0,int(row['experience'] or 0)))
        completed=min(50,int(row['completed_jobs'] or 0))
        completeness=sum(1 for k in ('phone','profile_photo_path','bio','skills','experience','ekyc_status') if row[k] not in (None,'',0)) / 6.0
        # Transparent trust score: rating 50%, experience 20%, profile completeness 15%, completed jobs 15%.
        trust=round((rating/5)*50 + (experience/10)*20 + completeness*15 + (completed/50)*15,1)
        # Ranking favors trust first, then proximity. Customers still see all eligible providers.
        rank=(trust, rating, -float(distance if distance is not None else 999999))
        out.append({
            'provider_id':pid,'user_id':int(row['user_id']),'name':row['name'],'phone':row['phone'],
            'masked_phone':_masked_phone(row['phone']),'profile_photo_path':row['profile_photo_path'],
            'skills':row['skills'] or 'Professional services','experience':experience,'rating':round(rating,1),
            'review_count':int(row['review_count'] or 0),'completed_jobs':completed,'bio':row['bio'],
            'ekyc_status':row['ekyc_status'] or 'NOT_SUBMITTED','is_online':bool(row['is_online']),
            'pincode':pin,'location_source':source,'latitude':row['latitude'],'longitude':row['longitude'],
            'distance_km':round(distance,2) if distance is not None else None,
            'eta_minutes':max(1,round(distance/0.35)) if distance is not None else None,
            'trust_score':trust,'profile_completeness':round(completeness*100),
            'same_pin':same_pin,'_rank':rank
        })
    out.sort(key=lambda x:(-x['_rank'][0],-x['_rank'][1],x['_rank'][2]))
    for x in out: x.pop('_rank',None)
    return out[:max(1,min(int(limit or 50),100))]

def diagnose(conn, request_id, radius_km=None):
    """Return explainable eligibility diagnostics for local testing/debugging."""
    req=conn.execute("""SELECT sr.*,s.name AS service_name FROM service_requests sr JOIN services s ON s.id=sr.service_id WHERE sr.id=?""",(request_id,)).fetchone()
    if not req: return []
    radius=float(radius_km or req["service_radius_km"] or 10)
    rows=conn.execute("""SELECT p.id AS provider_id,p.skills,p.approved,u.name,u.is_online,u.latitude,u.longitude,u.location_source,u.pincode,u.location_updated_at FROM providers p JOIN users u ON u.id=p.user_id WHERE u.role='provider' ORDER BY p.id""").fetchall()
    out=[]
    service=(req["service_name"] or '').strip().lower()
    for r in rows:
        raw=(r["skills"] or '').lower()
        tokens={x.strip() for x in re.split(r'[,;|]',raw) if x.strip()}
        same_pin=bool(PIN_TEST_MODE and (r["location_source"] or '').upper()=='PINCODE' and str(r["pincode"] or '')==str(req["customer_pincode"] or ''))
        d=None
        if r["latitude"] is not None and r["longitude"] is not None and req["customer_latitude"] is not None and req["customer_longitude"] is not None:
            d=round(haversine(req["customer_latitude"],req["customer_longitude"],r["latitude"],r["longitude"]),2)
        out.append({"provider_id":r["provider_id"],"name":r["name"],"approved":bool(r["approved"]),"online":bool(r["is_online"]),"source":r["location_source"],"pincode":r["pincode"],"same_pin":same_pin,"distance_km":d,"service_match":service in tokens,"eligible":bool(r["approved"] and r["is_online"] and r["latitude"] is not None and r["longitude"] is not None and service in tokens and (same_pin or (d is not None and d<=radius)))})
    return out

def start_wave(conn, request_id, radius_km=None):
    req=conn.execute("SELECT * FROM service_requests WHERE id=?",(request_id,)).fetchone()
    if not req:
        return {"ok":False,"message":"Request not found"}
    if req["status"] not in ("PENDING","SEARCHING","OFFERED"):
        return {"ok":False,"message":"Request is not searchable"}
    radius=float(radius_km or req["service_radius_km"] or 10)
    radius=max(1,min(radius,MAX_RADIUS_KM))
    next_wave=int(req["matching_wave"] or 0)+1
    picks=candidates(conn,request_id,radius,WAVE_SIZE)
    if not picks:
        now=utcnow()
        retry=now+timedelta(seconds=10)
        conn.execute("""
            UPDATE service_requests
            SET status='SEARCHING',search_started_at=COALESCE(search_started_at,?),
                search_deadline=?,matching_wave=?,service_radius_km=?
            WHERE id=?
        """,(now.isoformat(" "),retry.isoformat(" "),next_wave,radius,request_id))
        return {"ok":False,"message":"No fresh, online providers are available in this radius; SmartServe will expand the search.","count":0,"wave":next_wave,"radius_km":radius,"retry_at":retry.isoformat(" ")}
    now=utcnow()
    expires=now+timedelta(seconds=OFFER_TTL_SECONDS)
    for pick in picks:
        conn.execute("""
            INSERT INTO match_offers(request_id,provider_id,wave,status,offered_at,expires_at,distance_km,eta_minutes)
            VALUES(?,?,?,'OFFERED',?,?,?,?)
        """,
        (request_id,pick["provider"]["provider_id"],next_wave,now.isoformat(" "),expires.isoformat(" "),pick["distance_km"],pick["eta_minutes"]))
    conn.execute("""
        UPDATE service_requests
        SET status='SEARCHING',search_started_at=COALESCE(search_started_at,?),
            search_deadline=?,matching_wave=?,service_radius_km=?
        WHERE id=?
    """,(now.isoformat(" "),expires.isoformat(" "),next_wave,radius,request_id))
    return {"ok":True,"wave":next_wave,"radius_km":radius,"offers":picks,"expires_at":expires.isoformat(" ")}

def accept_offer(conn, request_id, provider_id):
    # Transaction must be held by caller. The WHERE clause is the race-condition guard.
    now=utcnow().isoformat(" ")
    offer=conn.execute("""
        SELECT * FROM match_offers
        WHERE request_id=? AND provider_id=? AND status='OFFERED'
        ORDER BY id DESC LIMIT 1
    """,(request_id,provider_id)).fetchone()
    if not offer:
        return False,"This request is no longer available."
    exp=parse_dt(offer["expires_at"])
    if not exp or exp < utcnow():
        conn.execute("UPDATE match_offers SET status='EXPIRED',responded_at=? WHERE id=?",(now,offer["id"]))
        return False,"This offer has expired."
    cur=conn.execute("""
        UPDATE service_requests
        SET provider_id=?,status='ACCEPTED',accepted_at=?,search_deadline=NULL
        WHERE id=? AND provider_id IS NULL AND status IN ('SEARCHING','OFFERED','ASSIGNED')
    """,(provider_id,now,request_id))
    if cur.rowcount!=1:
        return False,"Another provider already accepted this request."
    conn.execute("UPDATE match_offers SET status='ACCEPTED',responded_at=? WHERE id=?",(now,offer["id"]))
    conn.execute("UPDATE match_offers SET status='CANCELLED',responded_at=? WHERE request_id=? AND id<>? AND status='OFFERED'",(now,request_id,offer["id"]))
    return True,"Service request accepted."

def reject_offer(conn, request_id, provider_id):
    cur=conn.execute("""
        UPDATE match_offers SET status='REJECTED',responded_at=CURRENT_TIMESTAMP
        WHERE request_id=? AND provider_id=? AND status='OFFERED'
    """,(request_id,provider_id))
    return cur.rowcount>0

def expire_and_requeue(conn):
    now=utcnow()
    changed=[]
    offers=conn.execute("""
        SELECT id,request_id FROM match_offers
        WHERE status='OFFERED' AND expires_at<=?
    """,(now.isoformat(" "),)).fetchall()
    for row in offers:
        conn.execute("UPDATE match_offers SET status='EXPIRED',responded_at=? WHERE id=? AND status='OFFERED'",(now.isoformat(" "),row["id"]))
        changed.append(int(row["request_id"]))
    reqs=conn.execute("""
        SELECT * FROM service_requests
        WHERE status IN ('SEARCHING','OFFERED') AND search_deadline IS NOT NULL AND search_deadline<=?
    """,(now.isoformat(" "),)).fetchall()
    for req in reqs:
        active=conn.execute("SELECT COUNT(*) c FROM match_offers WHERE request_id=? AND status='OFFERED'",(req["id"],)).fetchone()["c"]
        if active:
            continue
        current=float(req["service_radius_km"] or 10)
        next_radius=min(MAX_RADIUS_KM, current*2)
        result=start_wave(conn,int(req["id"]),next_radius)
        if result.get("ok"):
            changed.append(int(req["id"]))
        elif current>=MAX_RADIUS_KM:
            conn.execute("UPDATE service_requests SET status='EXPIRED',search_deadline=NULL WHERE id=? AND status IN ('SEARCHING','OFFERED')",(req["id"],))
            changed.append(int(req["id"]))
    return sorted(set(changed))
